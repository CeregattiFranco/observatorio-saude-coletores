#!/usr/bin/env python3
"""deploy/painel/ripsa.py · indicadores RIPSA do Ministério da Saúde para TODOS os municípios (02/10/2026).

Fonte: dadosabertos.saude.gov.br (conjuntos "RIPSA"), arquivos anuais em CSV zipado no S3 do Ministério:
  nascimentos (SINASC)  baixo peso ao nascer, prematuridade, pré-natal com 6 ou mais consultas e iniciado no 1º trimestre, cesárea, mãe adolescente
                        (numeradores e denominadores por município: a região de saúde, a UF e o Brasil são somas)
  mortalidade (SIM)     taxas por 100 mil habitantes já calculadas pelo Ministério para município, região de saúde, UF e
                        Brasil: doenças crônicas prematuras (30–69 anos), cardiovasculares, câncer, diabetes, respiratórias
                        crônicas. Mortalidade infantil e neonatal: óbitos dos microdados anuais do SIM (S3 do OpenDataSUS)
                        ÷ nascidos vivos do SINASC, para cada nível.
  internacoes (SIH)     internações por condições sensíveis à atenção primária por 10 mil habitantes: total e os grupos
                        ligados a alimentação e doença crônica (somas por município, como os nascimentos)
Grava painel.regiao_indicador (fontes 'nascimentos', 'mortalidade' e 'internacoes'), uma linha por município. Os arquivos
mudam uma vez por ano; rodar uma vez por mês basta. Uso:
  python3 ripsa.py                    baixa, agrega e grava (~6 min, ~600 MB em /tmp, apagados ao fim)
  python3 ripsa.py --simular X.json   não grava; escreve o resultado de --so (ou de Campinas) em X.json
  python3 ripsa.py --so 350950        só esse município (teste)
"""
import shlex
import csv, datetime as dt, gzip, io, itertools, json, os, re, subprocess, sys, tempfile, urllib.error, urllib.request, zipfile

S3 = 'https://demas-dados-abertos.s3.amazonaws.com/csv/'
ANOS = 6
# Banco: o comando psql vem de OBSERVATORIO_PSQL (padrão 'psql'); usuário/banco pelas variáveis padrão PGHOST, PGUSER, PGDATABASE.
PSQL = shlex.split(os.environ.get('OBSERVATORIO_PSQL', 'psql'))

UA = 'anaceregatti-observatorio/1.0 (+https://www.anaceregatti.com.br/blog/biblioteca/dados-publicos-saude)'

# nascimentos: arquivo → [(id, nome, numerador(linha), coluna do denominador)]
def _soma(*cols):
    return lambda x: sum(num(x[c]) for c in cols)


NASCIMENTOS = {
    'mgdi_ms_r5h': [('baixo_peso', 'Nascidos com menos de 2,5 kg', _soma('Numerador - Número NV baixo peso'), 'Demoninador - Número de nascidos vivos'),
                    ('prematuro', 'Nascidos antes de 37 semanas', _soma('Denominador - Número NV pré-termo'), 'Demoninador - Número de nascidos vivos')],
    'ripsa003cb': [('pre_natal_6', 'Mães com 6 ou mais consultas de pré-natal', _soma('Numerador - Nascidos vivos c/6+ consultas'), 'Denominador - Nascidos vivos'),
                   ('pre_natal_1tri', 'Pré-natal iniciado no 1º trimestre',
                    lambda x: num(x['Denominador - Nascidos vivos']) if x.get('Início do pré-natal', '').startswith('1') else 0.0, 'Denominador - Nascidos vivos')],
    'ripsa005cb': [('cesarea', 'Partos cesáreos', _soma('Numerador - Parto cesáreos'), 'Denominador - Nascidos vivos')],
    'mgdi_ms_au5': [('mae_adolescente', 'Mães com menos de 20 anos', _soma('Numerador - Número de parturientes até 10 anos', 'Numerador - Número de parturientes de 10 a 14 anos',
                                                                      'Numerador - Número de parturientes de 15 a 19 anos'), 'Demoninador - Número de nascidos vivos')],
}
# mortalidade: arquivo MGDI → (id, nome). Linha "TC" = total.
MORTALIDADE = {'ripsa002mt': ('dcnt_prematura', 'Mortes de 30 a 69 anos por doenças crônicas'),
               'mgdi_ms_4v1': ('cardiovascular', 'Doenças cardiovasculares'),
               'mgdi_ms_7um': ('cancer', 'Câncer (neoplasias malignas)'),
               'mgdi_ms_bbt': ('diabetes', 'Diabetes'),
               'mgdi_ms_h0w': ('respiratoria', 'Doenças respiratórias crônicas')}
# internações por condições sensíveis à atenção primária (lista da Portaria SAS 221/2008; grupos na ordem da portaria).
# Linhas por município × sexo × faixa etária: soma tudo; o denominador é a população estimada.
ICSAP = 'mgdi_ms_qu3'
_CSAP = 'Numerador - Número internaçoes CSAP'
INTERNACOES = (('icsap', 'Todas as condições sensíveis', _CSAP), ('diabetes', 'Diabetes', _CSAP + ' - Grupo 13'),
               ('hipertensao', 'Hipertensão', _CSAP + ' - Grupo 9'), ('insuficiencia_cardiaca', 'Insuficiência cardíaca', _CSAP + ' - Grupo 11'),
               ('avc', 'AVC e outras doenças cerebrovasculares', _CSAP + ' - Grupo 12'),
               ('gastroenterite', 'Gastroenterites (diarreia e desidratação)', _CSAP + ' - Grupo 2'),
               ('nutricional', 'Anemia e deficiências nutricionais', (_CSAP + ' - Grupo 3', _CSAP + ' - Grupo 4')),
               ('asma', 'Asma', _CSAP + ' - Grupo 7'))
# mortalidade infantil e neonatal: óbitos dos microdados do SIM (por residência) ÷ nascidos vivos dos arquivos RIPSA acima.
# Sem a correção de sub-registro que o Ministério aplica na taxa oficial por UF (por isso fica um pouco abaixo dela).
SIM = 'https://s3.sa-east-1.amazonaws.com/ckan.saude.gov.br/SIM/csv/'
INFANTIS = (('infantil', 'Mortalidade infantil (menores de 1 ano)'), ('neonatal', 'Mortalidade neonatal (até 27 dias)'))

SIMULAR = '--simular' in sys.argv
SO = sys.argv[sys.argv.index('--so') + 1] if '--so' in sys.argv else None


def log(msg):
    print(f'{dt.datetime.now().astimezone().strftime("%d/%m %H:%M:%S")} ripsa: {msg}', flush=True)


def baixar(nome, pasta):
    caminho = os.path.join(pasta, nome + '.zip')
    with urllib.request.urlopen(urllib.request.Request(S3 + nome + '.csv.zip', headers={'User-Agent': UA}), timeout=300) as r, open(caminho, 'wb') as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    return caminho


def baixar_sim(ano, pasta):
    """Arquivo anual de óbitos do SIM; o nome muda conforme o ano (Mortalidade_Geral_AAAA ou DOaaOPEN)."""
    for nome in (f'Mortalidade_Geral_{ano}_csv.zip', f'DO{ano % 100:02d}OPEN_csv.zip'):
        caminho = os.path.join(pasta, nome)
        try:
            with urllib.request.urlopen(urllib.request.Request(SIM + nome, headers={'User-Agent': UA}), timeout=600) as r, open(caminho, 'wb') as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            return caminho
        except urllib.error.HTTPError:
            continue
    return None


def obitos_infantis(caminho):
    """{ibge6: [menores de 1 ano, até 27 dias]}. Os arquivos variam: com ou sem cabeçalho, com ou sem a coluna 'contador'
    na frente — a posição da data do óbito (dd-mm-aaaa ou ddmmaaaa) diz o deslocamento."""
    conta = {}
    z = zipfile.ZipFile(caminho)
    with z.open(z.namelist()[0]) as fh:
        leitor = csv.reader(io.TextIOWrapper(fh, encoding='latin-1', errors='replace'), delimiter=';')
        primeira = next(leitor)
        if 'IDADE' in primeira:
            tipo, idade, res = (primeira.index(c) for c in ('TIPOBITO', 'IDADE', 'CODMUNRES'))
            linhas = leitor
        else:
            d = 0 if re.fullmatch(r'\d{2}-?\d{2}-?\d{4}', primeira[2]) else 1
            tipo, idade, res = 1 + d, 7 + d, 15 + d
            linhas = itertools.chain([primeira], leitor)
        for x in linhas:
            if len(x) <= res or x[tipo] != '2':
                continue
            i = x[idade]
            if len(i) != 3 or not i.isdigit() or i[0] > '3' or (i[0] == '3' and int(i[1:]) >= 12):
                continue
            c = conta.setdefault(x[res][:6], [0, 0])
            c[0] += 1
            if i[0] in '01' or (i[0] == '2' and int(i[1:]) < 28):
                c[1] += 1
    return conta


def linhas_csv(caminho):
    z = zipfile.ZipFile(caminho)
    with z.open(z.namelist()[0]) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding='utf-8', errors='replace'))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def taxa(n, d, fator=100):
    return round(n / d * fator, 1) if d else None


def main():
    pasta = tempfile.mkdtemp(prefix='ripsa-')
    regiao, uf_de = {}, {}          # ibge6 → (código, nome da região de saúde); ibge6 → sigla
    mort = {}                       # (id, ibge6) → {ano: {municipio, regiao, estado, brasil}}
    nasc = {}                       # (id, ibge6, ano) → [numerador, denominador] · nascimentos e internações
    obitos = {}                     # ano → {ibge6: [menores de 1 ano, até 27 dias]}
    ultimo = {}
    try:
        for arq, (mid, _) in MORTALIDADE.items():
            for x in linhas_csv(baixar(arq, pasta)):
                if x.get('sg_categoria') != 'TC':
                    continue
                ib, ano = x['co_ibge'], int(x['co_anomes'][:4])
                regiao[ib] = (x['co_regiao_saude'], (x['no_regiao_saude'] or '').strip())
                uf_de[ib] = x['sg_uf']
                mort.setdefault((mid, ib), {})[ano] = {k: round(num(x['vl_indicador_calculado_' + c]), 1) for k, c in
                                                       (('municipio', 'mun'), ('regiao', 'rs'), ('estado', 'uf'), ('brasil', 'br'))}
                ultimo[mid] = max(ultimo.get(mid, 0), ano)
            os.remove(os.path.join(pasta, arq + '.zip'))
            log(f'{arq}: até {ultimo.get(mid)}')
        for arq, defs in NASCIMENTOS.items():
            for x in linhas_csv(baixar(arq, pasta)):
                ano = int(x['ano'])
                ib = x['Municipio']
                for nid, _, fnum, den in defs:
                    k = (nid, ib, ano)
                    c = nasc.setdefault(k, [0.0, 0.0])
                    c[0] += fnum(x)
                    c[1] += num(x[den])
                    ultimo[nid] = max(ultimo.get(nid, 0), ano)
            os.remove(os.path.join(pasta, arq + '.zip'))
            log(f'{arq}: até {max(ultimo.get(d[0], 0) for d in defs)}')
        desde = dt.date.today().year - ANOS - 3
        for x in linhas_csv(baixar(ICSAP, pasta)):
            ano = int(x['ano'] or 0)
            if ano < desde:
                continue
            ib, pop = x['Municipio'], num(x['Denominador - Populaçao estimada'])
            for iid, _, col in INTERNACOES:
                c = nasc.setdefault((iid, ib, ano), [0.0, 0.0])
                c[0] += sum(num(x[k]) for k in col) if isinstance(col, tuple) else num(x[col])
                c[1] += pop
                ultimo[iid] = max(ultimo.get(iid, 0), ano)
        os.remove(os.path.join(pasta, ICSAP + '.zip'))
        log(f'{ICSAP}: até {ultimo.get("icsap")}')
        for ano in range(ultimo.get('baixo_peso', 0) - ANOS + 1, ultimo.get('baixo_peso', 0) + 1):
            caminho = baixar_sim(ano, pasta)
            if caminho:
                obitos[ano] = obitos_infantis(caminho)
                os.remove(caminho)
                log(f'SIM {ano}: {sum(v[0] for v in obitos[ano].values())} óbito(s) de menores de 1 ano')
    finally:
        for f in os.listdir(pasta):
            os.remove(os.path.join(pasta, f))
        os.rmdir(pasta)

    # somas por região de saúde, UF e Brasil
    agg = {}
    for (nid, ib, ano), (n, d) in nasc.items():
        for chave in (('m', ib), ('r', regiao.get(ib, (None,))[0]), ('u', ib[:2]), ('b', '')):
            if chave[1] is None:
                continue
            a = agg.setdefault((nid, chave, ano), [0.0, 0.0])
            a[0] += n
            a[1] += d

    obi = {}                        # (chave, ano) → [menores de 1 ano, até 27 dias]
    for ano, por in obitos.items():
        for ib, (i, n) in por.items():
            for chave in (('m', ib), ('r', regiao.get(ib, (None,))[0]), ('u', ib[:2]), ('b', '')):
                if chave[1] is None:
                    continue
                o = obi.setdefault((chave, ano), [0, 0])
                o[0] += i
                o[1] += n

    def infantis(ib, rg):
        """Taxas por mil nascidos vivos, ano a ano, e o último triênio somado (estável em cidade pequena)."""
        saida = []
        for k, (iid, nome) in enumerate(INFANTIS):
            pts = []
            for ano in sorted(obitos):
                vals = {}
                for rot, ch in (('municipio', ('m', ib)), ('regiao', ('r', rg[0])), ('estado', ('u', ib[:2])), ('brasil', ('b', ''))):
                    nv = (agg.get(('baixo_peso', ch, ano)) or [0, 0])[1]
                    vals[rot] = taxa((obi.get((ch, ano)) or [0, 0])[k], nv, 1000) if nv else None
                if vals['municipio'] is None:
                    continue
                pts.append(dict(ano=ano, obitos=(obi.get((('m', ib), ano)) or [0, 0])[k], **vals))
            if not pts:
                continue
            tri = pts[-3:]
            nv3 = sum((agg.get(('baixo_peso', ('m', ib), p['ano'])) or [0, 0])[1] for p in tri)
            saida.append({'id': iid, 'nome': nome, 'unidade': 'por mil nascidos vivos', 'pontos': pts,
                          'trienio': {'de': tri[0]['ano'], 'ate': tri[-1]['ano'], 'obitos': sum(p['obitos'] for p in tri),
                                      'municipio': taxa(sum(p['obitos'] for p in tri), nv3, 1000)}})
        return saida

    def anos(fim):
        return list(range(fim - ANOS + 1, fim + 1))

    municipios = sorted(set(regiao) | {ib for (_, ib, _) in nasc if len(ib) == 6 and not ib.endswith('0000')})
    if SO:
        municipios = [m for m in municipios if m == SO[:6]]
    saida = {}
    for ib in municipios:
        uf = uf_de.get(ib)
        rg = regiao.get(ib, (None, None))
        def serie(nid, fator, conta, k):
            pts = []
            for ano in anos(ultimo.get(nid, 0)):
                m = agg.get((nid, ('m', ib), ano))
                if not m or not m[1]:
                    continue
                r_, u_, b_ = (agg.get((nid, ch, ano)) for ch in (('r', rg[0]), ('u', ib[:2]), ('b', '')))
                pts.append({'ano': ano, 'municipio': taxa(*m, fator), conta: int(m[k]), 'regiao': taxa(*r_, fator) if r_ else None,
                            'estado': taxa(*u_, fator) if u_ else None, 'brasil': taxa(*b_, fator) if b_ else None})
            return pts

        nas = [{'id': nid, 'nome': nome, 'unidade': '%', 'pontos': pts} for defs in NASCIMENTOS.values() for nid, nome, _, _ in defs
               if (pts := serie(nid, 100, 'nascidos', 1))]
        intn = [{'id': iid, 'nome': nome, 'unidade': 'por 10 mil habitantes', 'pontos': pts} for iid, nome, _ in INTERNACOES
                if (pts := serie(iid, 10000, 'internacoes', 0))]
        mor = []
        for arq, (mid, nome) in MORTALIDADE.items():
            s = mort.get((mid, ib), {})
            pts = [dict(ano=a, **s[a]) for a in anos(ultimo.get(mid, 0)) if a in s]
            if pts:
                mor.append({'id': mid, 'nome': nome, 'unidade': 'por 100 mil habitantes', 'pontos': pts})
        mor += infantis(ib, rg)
        base = {'uf': uf, 'regiao': rg[1], 'regiao_codigo': rg[0]}
        saida[ib] = {'nascimentos': dict(base, indicadores=nas) if nas else None, 'mortalidade': dict(base, indicadores=mor) if mor else None,
                     'internacoes': dict(base, indicadores=intn) if intn else None}
    log(f'{len(saida)} município(s)')

    if SIMULAR:
        destino = sys.argv[sys.argv.index('--simular') + 1]
        alvo = SO[:6] if SO else '350950'
        with open(destino, 'w') as f:
            json.dump(saida.get(alvo), f, ensure_ascii=False, indent=1)
        log(f'simulado: {alvo} → {destino}')
        return
    gravar(saida)


def gravar(saida):
    """Uma linha por município e fonte em painel.regiao_indicador (a chave é o IBGE de 7 dígitos, como no resto do painel)."""
    with urllib.request.urlopen(urllib.request.Request('https://servicodados.ibge.gov.br/api/v1/localidades/municipios', headers={'User-Agent': UA}), timeout=120) as r:
        corpo = r.read()
    if corpo[:2] == b'\x1f\x8b':
        corpo = gzip.decompress(corpo)
    sete = {str(m['id'])[:6]: str(m['id']) for m in json.loads(corpo)}
    lote, total = [], 0
    for ib, fontes in saida.items():
        ib7 = sete.get(ib)
        if not ib7:
            continue
        for fonte, d in fontes.items():
            if d is None:
                continue
            lote.append(f"INSERT INTO painel.regiao_indicador (ibge, fonte, dados, atualizado_em) VALUES ('{ib7}', '{fonte}', $j${json.dumps(d, ensure_ascii=False)}$j$::jsonb, now()) "
                        "ON CONFLICT (ibge, fonte) DO UPDATE SET dados = EXCLUDED.dados, atualizado_em = now();")
        if len(lote) >= 800:
            total += executar(lote)
            lote = []
    total += executar(lote)
    log(f'{total} linha(s) gravada(s)')


def executar(lote):
    if not lote:
        return 0
    r = subprocess.run(PSQL + ['-v', 'ON_ERROR_STOP=1', '-q'],
                       input='BEGIN;\n' + '\n'.join(lote) + '\nCOMMIT;\n', capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f'psql: {r.stderr.strip()[:300]}')
    return len(lote)


if __name__ == '__main__':
    main()
