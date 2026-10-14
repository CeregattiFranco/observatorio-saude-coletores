#!/usr/bin/env python3
"""deploy/painel/consumo.py · marcadores de consumo alimentar do SISVAN por região de saúde (02/10/2026).

Fonte: relatório público do SISVAN (sisaps.saude.gov.br/sisvan/relatoriopublico, formulário de consumo alimentar
"a partir de 2015"). Para o município o relatório devolve a REGIÃO DE SAÚDE (todas as do estado de uma vez), com
o total do estado e do Brasil; por município as amostras são de poucas pessoas e as planilhas prontas param em 2022.
Cada consulta leva ~5 min no servidor do Ministério: cada rodada faz UMA (estado × indicador do último ano fechado),
guarda em painel.sisvan_consumo e regrava a fonte 'consumo' dos municípios daquele estado em painel.regiao_indicador.
A região de cada município vem da fonte 'nascimentos' (ripsa.py), casada pelo nome. Uso:
  python3 consumo.py                 uma consulta pendente (ou a mais antiga, se tudo tiver menos de 120 dias)
  python3 consumo.py --uf SP         a próxima pendente desse estado (SP só sai assim: fora da rodada automática)
  python3 consumo.py --so-regravar   só regrava a fonte 'consumo' com o que já foi coletado
"""
import os, shlex
import datetime as dt, html, http.cookiejar, json, re, subprocess, sys, time, unicodedata, urllib.parse, urllib.request

BASE = 'https://sisaps.saude.gov.br/sisvan/relatoriopublico/'
# Banco: o comando psql vem de OBSERVATORIO_PSQL (padrão 'psql'); usuário/banco pelas variáveis padrão PGHOST, PGUSER, PGDATABASE.
PSQL = shlex.split(os.environ.get('OBSERVATORIO_PSQL', 'psql'))

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36'
UFS = {'11': 'RO', '12': 'AC', '13': 'AM', '14': 'RR', '15': 'PA', '16': 'AP', '17': 'TO', '21': 'MA', '22': 'PI', '23': 'CE',
       '24': 'RN', '25': 'PB', '26': 'PE', '27': 'AL', '28': 'SE', '29': 'BA', '31': 'MG', '32': 'ES', '33': 'RJ', '35': 'SP',
       '41': 'PR', '42': 'SC', '43': 'RS', '50': 'MS', '51': 'MT', '52': 'GO', '53': 'DF'}
# SP fica fora da rodada automática: o relatório do estado passa dos ~300 s e o servidor corta (mesmo por semestre).
FORA_DA_RODADA = {'35'}
ORDEM_UF = sorted(c for c in UFS if c not in FORA_DA_RODADA)
# o formulário quer um município (a capital); a resposta traz todas as regiões de saúde do estado. Com o filtro
# "estado" o relatório de SP passou dos ~5 min e o servidor cortou a conexão.
CAPITAL = {'11': '110020', '12': '120040', '13': '130260', '14': '140010', '15': '150140', '16': '160030', '17': '172100',
           '21': '211130', '22': '221100', '23': '230440', '24': '240810', '25': '250750', '26': '261160', '27': '270430',
           '28': '280030', '29': '292740', '31': '310620', '32': '320530', '33': '330455', '35': '355030', '41': '410690',
           '42': '420540', '43': '431490', '50': '500270', '51': '510340', '52': '520870', '53': '530010'}
VALIDADE = 120   # dias: o ano fechado ainda recebe registros atrasados

# indicador → (nome, quem, campos do formulário)
INDICADORES = {
    'ame': ('Aleitamento materno exclusivo', 'menores de 6 meses', [('TP_RELATORIO5', '1'), ('MENOR_6_MESES5', '1')]),
    'ultra_bebe': ('Comeram ultraprocessados', 'bebês de 6 a 23 meses',
                   [('TP_RELATORIO5', '2'), ('ENTRE_6_2_ANOS5', '2'), ('OPCAO_ENTRE_6_2_ANOS5', '6')]),
    'ultra_crianca': ('Comeram ultraprocessados', 'crianças de 2 a 4 anos',
                      [('TP_RELATORIO5', '3'), ('MAIOR_2_ANOS5', '1'), ('OPCOES_MAIOR_2_ANOS5', '10')]),
    'feijao': ('Comeram feijão', 'adultos', [('TP_RELATORIO5', '3'), ('MAIOR_2_ANOS5', '4'), ('OPCOES_MAIOR_2_ANOS5', '3')]),
    'fruta': ('Comeram fruta', 'adultos', [('TP_RELATORIO5', '3'), ('MAIOR_2_ANOS5', '4'), ('OPCOES_MAIOR_2_ANOS5', '4')]),
    'verdura': ('Comeram verduras e legumes', 'adultos', [('TP_RELATORIO5', '3'), ('MAIOR_2_ANOS5', '4'), ('OPCOES_MAIOR_2_ANOS5', '5')]),
    'ultra_adulto': ('Comeram ultraprocessados', 'adultos', [('TP_RELATORIO5', '3'), ('MAIOR_2_ANOS5', '4'), ('OPCOES_MAIOR_2_ANOS5', '10')]),
}

SQL_TABELA = '''CREATE TABLE IF NOT EXISTS painel.sisvan_consumo (
  uf text NOT NULL, indicador text NOT NULL, ano int NOT NULL, dados jsonb NOT NULL, lido_em timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (uf, indicador, ano));
CREATE TABLE IF NOT EXISTS painel.sisvan_consumo_falha (
  uf text NOT NULL, indicador text NOT NULL, motivo text, em timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (uf, indicador));'''


def log(msg):
    print(f'{dt.datetime.now().astimezone().strftime("%d/%m %H:%M:%S")} consumo: {msg}', flush=True)


def psql(sql, tabular=False):
    r = subprocess.run(PSQL + ['-v', 'ON_ERROR_STOP=1', '-q', '-At', '-F', '\t'],
                       input=sql, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f'psql: {r.stderr.strip()[:300]}')
    return [l.split('\t') for l in r.stdout.strip().split('\n') if l] if tabular else r.stdout


def norm(s):
    s = unicodedata.normalize('NFD', str(s or '')).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


def inteiro(s):
    try:
        return int(str(s).replace('.', '').strip())
    except ValueError:
        return None


def contagem(sim, total):
    return {'sim': sim, 'total': total, 'percentual': round(sim / total * 100, 1) if total else None}


def tabela(s):
    """HTML do relatório → {'regioes': {nome: contagem}, 'estado': contagem, 'brasil': contagem}."""
    t = next((x for x in re.findall(r'<table.*?</table>', s, re.S) if 'Abrang' in x), None)
    if not t:
        return None
    saida = {'regioes': {}}
    for tr in re.findall(r'<tr.*?</tr>', t, re.S):
        cel = [html.unescape(re.sub(r'<[^>]+>', '', c)).strip() for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.S)]
        cel = [c for c in cel if c]
        if len(cel) < 4 or not cel[-1].replace('.', '').isdigit() or not cel[-2].endswith('%'):
            continue
        sim, total = inteiro(cel[-3]), inteiro(cel[-1])
        if sim is None or total is None:
            continue
        rot = cel[-4]
        if rot.startswith('TOTAL ESTADO'):
            saida['estado'] = contagem(sim, total)
        elif rot.startswith('TOTAL BRASIL'):
            saida['brasil'] = contagem(sim, total)
        elif not rot.startswith('TOTAL') and len(cel) >= 8:
            saida['regioes'][rot] = contagem(sim, total)
    return saida if saida['regioes'] or 'estado' in saida else None


def consultar(uf, ind, ano):
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    op.addheaders = [('User-Agent', UA)]
    op.open(BASE + 'index', timeout=60).read()
    dados = [('tpRelatorio', '5'), ('coVisualizacao', '1'), ('nuAno', str(ano)), ('nuAno2015', str(ano)), ('nuMes[]', '99'),
             ('tpFiltro', 'M'), ('coRegiao', uf[0]), ('coUfIbge', uf), ('coMunicipioIbge', CAPITAL[uf]), ('noRegional', '99'),
             ('ds_sexo5', 'T'), ('ds_raca_cor5', '99'), ('co_sistema_origem4', '0'), ('CO_POVO_COMUNIDADE5', 'TODOS'),
             ('CO_ESCOLARIDADE5', 'TODOS'), ('verTela', '')] + INDICADORES[ind][2]
    s = op.open(BASE + 'consumoalimentar', urllib.parse.urlencode(dados).encode(), timeout=1200).read().decode('utf-8', 'ignore')
    return tabela(s)


def regravar(ufs=None):
    """Fonte 'consumo' dos municípios: os indicadores da região de saúde de cada um, com estado e Brasil."""
    col = {}
    for uf, ind, ano, dados in psql('SELECT uf, indicador, ano, dados FROM painel.sisvan_consumo', True):
        if ufs and uf not in ufs:
            continue
        a = col.setdefault(uf, {})
        if ind not in a or int(ano) > a[ind][0]:
            a[ind] = (int(ano), json.loads(dados))
    sql, casou, sem = [], 0, set()
    for ib, dados in psql("SELECT ibge, dados FROM painel.regiao_indicador WHERE fonte = 'nascimentos' AND dados IS NOT NULL", True):
        uf = ib[:2]
        if uf not in col:
            continue
        reg = json.loads(dados).get('regiao') or ''
        inds = []
        for ind, (nome, quem, _) in INDICADORES.items():
            if ind not in col[uf]:
                continue
            ano, d = col[uf][ind]
            r = next((v for k, v in d['regioes'].items() if norm(k) == norm(reg)), None)
            if r is None:
                sem.add((uf, reg))
            inds.append({'id': ind, 'nome': nome, 'quem': quem, 'ano': ano, 'regiao': r, 'estado': d.get('estado'), 'brasil': d.get('brasil')})
        if not inds:
            continue
        casou += any(i['regiao'] for i in inds)
        d = {'uf': UFS.get(uf), 'regiao': reg, 'indicadores': inds}
        sql.append(f"INSERT INTO painel.regiao_indicador (ibge, fonte, dados, atualizado_em) VALUES ('{ib}', 'consumo', $j${json.dumps(d, ensure_ascii=False)}$j$::jsonb, now()) "
                   "ON CONFLICT (ibge, fonte) DO UPDATE SET dados = EXCLUDED.dados, atualizado_em = now();")
    for i in range(0, len(sql), 800):
        psql('BEGIN;\n' + '\n'.join(sql[i:i + 800]) + '\nCOMMIT;\n')
    log(f"consumo regravado para {len(sql)} município(s), {casou} com a região achada" +
        (f"; regiões sem par: {', '.join(sorted(r for _, r in sem))[:300]}" if sem else ''))


def main():
    psql(SQL_TABELA)
    if '--so-regravar' in sys.argv:
        return regravar()
    ano = dt.date.today().year - 1
    lidos = {(u, i): (int(a), l) for u, i, a, l in psql('SELECT uf, indicador, max(ano), max(lido_em)::date FROM painel.sisvan_consumo GROUP BY 1, 2', True)}
    ordem = [sys.argv[sys.argv.index('--uf') + 1]] if '--uf' in sys.argv else ORDEM_UF
    ordem = [next((c for c, s in UFS.items() if s == u.upper()), u) for u in ordem]
    falhas = {(u, i): em for u, i, em in psql('SELECT uf, indicador, em FROM painel.sisvan_consumo_falha', True)}
    fila = [(u, i) for u in ordem for i in INDICADORES if lidos.get((u, i), (0,))[0] < ano]
    fila.sort(key=lambda x: (x in falhas, falhas.get(x, '')))   # o que falhou vai para o fim, o mais antigo primeiro
    if not fila:
        velhos = sorted(((l, u, i) for (u, i), (a, l) in lidos.items() if u in ordem and dt.date.fromisoformat(l) < dt.date.today() - dt.timedelta(days=VALIDADE)))
        fila = [(u, i) for _, u, i in velhos[:1]]
    if not fila:
        log('nada pendente')
        return
    uf, ind = fila[0]
    t = time.time()
    try:
        d = consultar(uf, ind, ano)
        motivo = None if d else 'relatório sem tabela'
    except Exception as e:  # noqa: BLE001
        d, motivo = None, f'{type(e).__name__}: {e}'[:200]
    if not d:
        psql(f"INSERT INTO painel.sisvan_consumo_falha (uf, indicador, motivo) VALUES ('{uf}', '{ind}', $m${motivo}$m$) "
             "ON CONFLICT (uf, indicador) DO UPDATE SET motivo = EXCLUDED.motivo, em = now();")
        raise SystemExit(f'{UFS[uf]} {ind} {ano}: {motivo} ({time.time() - t:.0f}s); vai para o fim da fila')
    psql(f"DELETE FROM painel.sisvan_consumo_falha WHERE uf = '{uf}' AND indicador = '{ind}';")
    psql(f"INSERT INTO painel.sisvan_consumo (uf, indicador, ano, dados) VALUES ('{uf}', '{ind}', {ano}, $j${json.dumps(d, ensure_ascii=False)}$j$::jsonb) "
         "ON CONFLICT (uf, indicador, ano) DO UPDATE SET dados = EXCLUDED.dados, lido_em = now();")
    log(f"{UFS[uf]} {ind} {ano}: {len(d['regioes'])} região(ões), estado {(d.get('estado') or {}).get('percentual')}%, {time.time() - t:.0f}s · faltam {len(fila) - 1}")
    regravar({uf})


if __name__ == '__main__':
    main()
