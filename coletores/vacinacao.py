#!/usr/bin/env python3
"""deploy/painel/vacinacao.py · cobertura vacinal infantil por município, a partir dos microdados do PNI (02/10/2026).

Fonte: dadosabertos.saude.gov.br, "Doses aplicadas pelo Programa Nacional de Imunizações" (um CSV zipado por mês,
1,5–3 GB). O arquivo é lido em streaming (nada vai para o disco) e só sobram contagens: doses de 7 vacinas do
calendário infantil, em crianças de até 2 anos, por município de RESIDÊNCIA. Cobertura = doses ÷ nascidos vivos do
município (SINASC mais recente, da fonte 'nascimentos' gravada por ripsa.py), como o Ministério calcula.

Tabelas: painel.regiao_vacina (contagem mensal) e painel.regiao_vacina_arquivo (mês lido + Last-Modified do arquivo).
Cada rodada lê UM mês ainda não lido (ou que mudou) e regrava a fonte 'vacinacao' de todos os municípios em
painel.regiao_indicador. Uso:
  python3 vacinacao.py                 um mês por rodada (~1–3 min de rede e CPU)
  python3 vacinacao.py --mes 2025-12   esse mês (relê mesmo se já lido)
  python3 vacinacao.py --so-recalcular só regrava a fonte 'vacinacao' com o que já está contado
"""
import os, shlex
import datetime as dt, json, struct, subprocess, sys, time, urllib.request, zlib

# Banco: o comando psql vem de OBSERVATORIO_PSQL (padrão 'psql'); usuário/banco pelas variáveis padrão PGHOST, PGUSER, PGDATABASE.
PSQL = shlex.split(os.environ.get('OBSERVATORIO_PSQL', 'psql'))

UA = 'anaceregatti-observatorio/1.0 (+https://www.anaceregatti.com.br/blog/biblioteca/dados-publicos-saude)'
URL = 'https://s3.sa-east-1.amazonaws.com/ckan.saude.gov.br/PNI/csv/vacinacao_{mes}_{ano}_csv.zip'
MESES = ['jan', 'fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez']

# vacina → (nome, meta do PNI, {(sigla do imunobiológico, tipo de dose)}); a 3ª dose da hexa conta para penta e pólio
VACINAS = {
    'bcg': ('BCG', 90, {('BCG', 'Única'), ('BCG', 'Dose')}),
    'penta': ('Pentavalente (3ª dose)', 95, {('Penta', '3ª Dose'), ('Hexa acelular', '3ª Dose'), ('Penta acelular', '3ª Dose')}),
    'polio': ('Pólio injetável (3ª dose)', 95, {('VIP', '3ª Dose'), ('Hexa acelular', '3ª Dose')}),
    'pneumo': ('Pneumocócica (2ª dose)', 95, {('VPC10', '2ª Dose'), ('VPC13', '2ª Dose'), ('VPC15', '2ª Dose'), ('VPC20', '2ª Dose')}),
    'rota': ('Rotavírus (última dose)', 90, {('ROTA', '2ª Dose'), ('ROTA5', '3ª Dose')}),
    'menc': ('Meningocócica C (2ª dose)', 95, {('MenC', '2ª Dose')}),
    'scr': ('Tríplice viral (1ª dose)', 95, {('SCR', '1ª Dose')}),
}
ALVO = {}
for _v, (_, _, pares) in VACINAS.items():
    for _p in pares:
        ALVO.setdefault((_p[0].encode('latin-1'), _p[1].encode('latin-1')), []).append(_v)

SQL_TABELAS = '''CREATE TABLE IF NOT EXISTS painel.regiao_vacina (
  ibge text NOT NULL, ano int NOT NULL, mes int NOT NULL, vacina text NOT NULL, doses int NOT NULL,
  PRIMARY KEY (ibge, ano, mes, vacina));
CREATE TABLE IF NOT EXISTS painel.regiao_vacina_arquivo (
  ano int NOT NULL, mes int NOT NULL, modificado text, linhas bigint, lido_em timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (ano, mes));'''


def log(msg):
    print(f'{dt.datetime.now().astimezone().strftime("%d/%m %H:%M:%S")} vacinacao: {msg}', flush=True)


def psql(sql, tabular=False):
    r = subprocess.run(PSQL + ['-v', 'ON_ERROR_STOP=1', '-q', '-At', '-F', '\t'],
                       input=sql, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f'psql: {r.stderr.strip()[:300]}')
    return [l.split('\t') for l in r.stdout.strip().split('\n') if l] if tabular else r.stdout


def modificado(ano, mes):
    req = urllib.request.Request(URL.format(mes=MESES[mes - 1], ano=ano), method='HEAD', headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.headers.get('Last-Modified')
    except Exception:  # noqa: BLE001
        return None


def contar(ano, mes):
    """Lê o zip em streaming (1 arquivo, deflate) → ({(ibge6, vacina): doses}, linhas lidas)."""
    r = urllib.request.urlopen(urllib.request.Request(URL.format(mes=MESES[mes - 1], ano=ano), headers={'User-Agent': UA}), timeout=300)
    cab = r.read(30)
    _, _, flag, metodo, _, _, _, _, _, nl, el = struct.unpack('<IHHHHHIIIHH', cab)
    if metodo != 8:
        raise SystemExit(f'zip {ano}-{mes:02d}: método {metodo} não suportado')
    r.read(nl + el)
    dec, resto, h, n, cont = zlib.decompressobj(-15), b'', None, 0, {}
    while True:
        b = r.read(1 << 20)
        if not b:
            break
        partes = (resto + dec.decompress(b)).split(b'\n')
        resto = partes.pop()
        for l in partes:
            c = l.split(b';')
            if h is None:
                h = {k.decode('latin-1').strip(): i for i, k in enumerate(c)}
                im, idose, imun, iid = h['sg_imunobiologico'], h['ds_tipo_dose'], h['co_municipio_paciente'], h['nu_idade_paciente']
                continue
            n += 1
            if len(c) <= iid:
                continue
            vs = ALVO.get((c[im], c[idose]))
            if not vs:
                continue
            try:
                if int(c[iid] or 99) > 2:
                    continue
            except ValueError:
                continue
            ib = c[imun].decode()
            for v in vs:
                cont[(ib, v)] = cont.get((ib, v), 0) + 1
        if dec.eof:
            break
    return cont, n


def gravar_mes(ano, mes, cont, n, mod):
    sql = [f'DELETE FROM painel.regiao_vacina WHERE ano = {ano} AND mes = {mes};']
    vals = [f"('{ib}', {ano}, {mes}, '{v}', {d})" for (ib, v), d in cont.items() if ib.isdigit()]
    for i in range(0, len(vals), 2000):
        sql.append('INSERT INTO painel.regiao_vacina (ibge, ano, mes, vacina, doses) VALUES ' + ', '.join(vals[i:i + 2000]) + ';')
    sql.append(f"INSERT INTO painel.regiao_vacina_arquivo (ano, mes, modificado, linhas, lido_em) VALUES ({ano}, {mes}, $m${mod or ''}$m$, {n}, now()) "
               "ON CONFLICT (ano, mes) DO UPDATE SET modificado = EXCLUDED.modificado, linhas = EXCLUDED.linhas, lido_em = now();")
    psql('BEGIN;\n' + '\n'.join(sql) + '\nCOMMIT;\n')


def recalcular():
    """Fonte 'vacinacao' de todos os municípios: cobertura por ano completo (12 meses) e o ano corrente parcial."""
    meses = {(int(a), int(m)) for a, m in psql('SELECT ano, mes FROM painel.regiao_vacina_arquivo', True)}
    doses = {}
    for ib, a, v, d in psql('SELECT ibge, ano, vacina, sum(doses) FROM painel.regiao_vacina GROUP BY 1, 2, 3', True):
        doses[(ib, int(a), v)] = int(d)
    nasc, reg, uf, ib7 = {}, {}, {}, {}
    for ib, dados in psql("SELECT ibge, dados FROM painel.regiao_indicador WHERE fonte = 'nascimentos' AND dados IS NOT NULL", True):
        d = json.loads(dados)
        pts = next((i['pontos'] for i in d.get('indicadores') or [] if i['id'] == 'baixo_peso'), [])
        if pts:
            nasc[ib[:6]] = (pts[-1]['ano'], pts[-1]['nascidos'])
            reg[ib[:6]], uf[ib[:6]], ib7[ib[:6]] = d.get('regiao_codigo'), d.get('uf'), ib
    if not nasc:
        log('sem nascimentos gravados (rode ripsa.py antes)')
        return
    anos = sorted({a for a, _ in meses})
    fracao = {ano: len([1 for a, _ in meses if a == ano]) / 12 for ano in anos}   # ano parcial: nascidos proporcionais aos meses lidos
    soma = {}
    for m, (_, nn) in nasc.items():
        for g in (('b', ''), ('u', uf[m]), ('r', reg[m])):
            for ano in anos:
                for v in VACINAS:
                    s = soma.setdefault(g + (ano, v), [0, 0])
                    s[0] += doses.get((m, ano, v), 0)
                    s[1] += nn * fracao[ano]
    agg = {k: round(d / n * 100, 1) if n else None for k, (d, n) in soma.items()}
    sql = []
    for m, (ano_n, nn) in nasc.items():
        anos_ = []
        for ano in anos:
            nmes = len([1 for a, _ in meses if a == ano])
            anos_.append({'ano': ano, 'meses': nmes, 'vacinas': [
                {'id': v, 'nome': VACINAS[v][0], 'meta': VACINAS[v][1], 'doses': doses.get((m, ano, v), 0),
                 'municipio': round(doses.get((m, ano, v), 0) / (nn * fracao[ano]) * 100, 1) if nn else None,
                 'regiao': agg.get(('r', reg[m], ano, v)), 'estado': agg.get(('u', uf[m], ano, v)), 'brasil': agg.get(('b', '', ano, v))}
                for v in VACINAS]})
        d = {'nascidos': {'ano': ano_n, 'total': nn}, 'anos': anos_}
        sql.append(f"INSERT INTO painel.regiao_indicador (ibge, fonte, dados, atualizado_em) VALUES ('{ib7[m]}', 'vacinacao', $j${json.dumps(d, ensure_ascii=False)}$j$::jsonb, now()) "
                   "ON CONFLICT (ibge, fonte) DO UPDATE SET dados = EXCLUDED.dados, atualizado_em = now();")
    for i in range(0, len(sql), 800):
        psql('BEGIN;\n' + '\n'.join(sql[i:i + 800]) + '\nCOMMIT;\n')
    log(f'cobertura regravada para {len(sql)} município(s) · anos {anos}')


def main():
    psql(SQL_TABELAS)
    if '--so-recalcular' in sys.argv:
        return recalcular()
    lidos = {(int(a), int(m)): mod for a, m, mod in psql('SELECT ano, mes, modificado FROM painel.regiao_vacina_arquivo', True)}
    if '--mes' in sys.argv:
        a, m = sys.argv[sys.argv.index('--mes') + 1].split('-')
        fila = [(int(a), int(m))]
    else:
        y = dt.date.today().year   # o último ano fechado primeiro, depois o anterior (comparação) e o corrente (parcial)
        fila = [(a, m) for a in (y - 1, y - 2, y) for m in range(1, 13) if (a, m) < (y, dt.date.today().month)]
    for ano, mes in fila:
        mod = modificado(ano, mes)
        if not mod or ('--mes' not in sys.argv and lidos.get((ano, mes)) == mod):
            continue
        t = time.time()
        cont, n = contar(ano, mes)
        gravar_mes(ano, mes, cont, n, mod)
        log(f'{ano}-{mes:02d}: {n} doses lidas, {sum(cont.values())} na conta, {time.time() - t:.0f}s')
        break
    else:
        log('nada novo')
    recalcular()


if __name__ == '__main__':
    main()
