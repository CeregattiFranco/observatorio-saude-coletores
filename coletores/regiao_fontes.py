"""Fontes públicas do observatório "Saúde na sua região" (29/09/2026). Só biblioteca padrão.

Usado pelos dois lados, com a mesma lógica:
  - regiao-saude.py (host, cron de hora em hora) grava em lote no Postgres;
  - servidor.py (contêiner do painel) busca na hora quando um visitante pede um município ainda sem dado.
Aqui só se busca e normaliza; quem grava é quem chama. Custo zero: APIs públicas, sem chave.

  infodengue(ibge)   dengue/chikungunya/zika + clima da semana (temperatura, umidade) · InfoDengue (Fiocruz/FGV)
  inmet_avisos()     avisos meteorológicos ativos do país (tempestade, chuvas intensas, vendaval, calor…) · INMET
  tempo(ibge)        previsão oficial por município (INMET, 5 dias) + agora e próximas 24 h (chuva, rajadas) · Open-Meteo
  publicacoes()      últimas publicações de saúde da população: Ministério da Saúde (notícias e boletins
                     epidemiológicos), OMS (notícias) e OPAS (notícias em português)

02/10/2026 — indicadores municipais (indicador(fonte, ibge, uf) → dados agregados; validade em INDICADORES):
  sisagua         amostras da vigilância da água para consumo nos últimos meses (SISAGUA) · API de Dados Abertos do MS
  zika            notificações de zika do ano (SINAN) — a InfoDengue não tem série de zika
  febre_amarela   casos humanos e epizootias do período de monitoramento (jul–jun), município e UF
  ubs             unidades básicas de saúde ativas (CNES)
  previne         indicadores de desempenho da atenção primária, último quadrimestre publicado (Previne Brasil)
  sisvan          estado nutricional das pessoas acompanhadas na atenção primária, último ano fechado (relatório público do SISVAN);
                  a série dos 5 anos fechados (adulto e criança) vira snapshot para tendência de sobrepeso e obesidade
  ibge_alimentacao segurança alimentar (PNAD Contínua, UF) e aquisição domiciliar de alimentos (POF, UF) · API de agregados do IBGE
  reservatorios    nível dos reservatórios monitorados pela ANA (SAR): os do município e os mais próximos
  lavoura          hortaliças e frutas da Produção Agrícola Municipal + estabelecimentos do Censo Agro · IBGE
  perfil_ibge      população, idade, cor ou raça, domicílios e saneamento do município · Censo 2022 e estimativa

03/10/2026 — Eco30 · meteorologia e saúde (/blog/biblioteca/eco30-meteorologia):
  calor           máximas de 30 dias atrás a 7 dias à frente × normal 1991–2020 (ERA5), onda de calor (INMET), sensação, UV · Open-Meteo
  ar              PM2.5, PM10, NO₂ e ozônio estimados (Copernicus CAMS) × diretrizes da OMS 2021 · Open-Meteo
  queimadas       focos dos últimos 7 dias no município e no estado (satélite de referência AQUA), dias sem chuva, risco de fogo · INPE

06/10/2026 — Eco30 · metabólicos (/blog/biblioteca/eco30-metabolicos):
  pns             hipertensão, diabetes e colesterol alto referidos (2013 e 2019) no Brasil, no estado e na capital;
                  obesidade e excesso de peso medidos, só Brasil · PNS/IBGE. O resto da página usa 'mortalidade' e
                  'internacoes' (ripsa.py) e 'sisvan'.
"""
import datetime as dt
import gzip
import html
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request

UA = 'anaceregatti-observatorio/1.0 (+https://www.anaceregatti.com.br/blog/biblioteca/dados-publicos-saude)'
BRT = dt.timezone(dt.timedelta(hours=-3))


def _get(url, timeout=25, tentativas=2, navegador=False):
    ua = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36' if navegador else UA
    for i in range(tentativas):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': ua, 'Accept-Encoding': 'identity'}), timeout=timeout) as r:
                b = r.read()
                return gzip.decompress(b) if b[:2] == b'\x1f\x8b' else b   # o IBGE manda gzip mesmo sem pedir
        except Exception:  # noqa: BLE001
            if i == tentativas - 1:
                raise
            time.sleep(2)


def _json(url, **k):
    return json.loads(_get(url, **k) or b'null')


def _num(x):
    try:
        return round(float(x), 1)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ InfoDengue
def infodengue(ibge):
    """[{doenca, semana, nivel, casos, casos_est, incidencia, dados[8 semanas com clima]}]"""
    ano, saida = dt.date.today().year, []
    for doenca in ('dengue', 'chikungunya', 'zika'):
        try:
            d = _json('https://info.dengue.mat.br/api/alertcity?' + urllib.parse.urlencode(
                {'geocode': ibge, 'disease': doenca, 'format': 'json', 'ew_start': 1, 'ew_end': 53, 'ey_start': ano - 1, 'ey_end': ano}), timeout=20)
        except Exception:  # noqa: BLE001
            d = None
        if not d:
            continue
        d = sorted(d, key=lambda x: x.get('SE') or 0)[-8:]
        u = d[-1]
        saida.append({'doenca': doenca, 'semana': u.get('SE'), 'nivel': u.get('nivel'), 'casos': u.get('casos'), 'casos_est': u.get('casos_est'),
                      'incidencia': u.get('p_inc100k'),
                      'dados': [{'SE': x.get('SE'), 'casos': x.get('casos'), 'casos_est': x.get('casos_est'), 'nivel': x.get('nivel'), 'p_inc100k': x.get('p_inc100k'),
                                 'Rt': _num(x.get('Rt')), 'tempmin': _num(x.get('tempmin')), 'tempmed': _num(x.get('tempmed')), 'tempmax': _num(x.get('tempmax')),
                                 'umidmin': _num(x.get('umidmin')), 'umidmed': _num(x.get('umidmed')), 'umidmax': _num(x.get('umidmax')),
                                 'receptivo': x.get('receptivo'), 'transmissao': x.get('transmissao')} for x in d]})
    return saida


# ------------------------------------------------------------------ INMET · avisos
def _lista_texto(s):
    """O INMET manda riscos/instruções como a repr de uma lista Python ("['a', 'b']")."""
    if isinstance(s, list):
        return [str(x).strip() for x in s if str(x).strip()]
    s = (s or '').strip()
    if s.startswith('['):
        itens = re.findall(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"", s)
        return [a or b for a, b in itens if (a or b).strip()]
    return [s] if s else []


def _data_inmet(data, hora):
    try:
        d = dt.date.fromisoformat(str(data)[:10])
        h, m = (hora or '00:00').split(':')[:2]
        return dt.datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=BRT)
    except Exception:  # noqa: BLE001
        return None


def inmet_avisos():
    """Avisos ativos de hoje e dos próximos dias, país inteiro: [{codigo, descricao, severidade, cor, inicio, fim, riscos, instrucoes, geocodes}]."""
    d = _json('https://apiprevmet3.inmet.gov.br/avisos/ativos', timeout=40, navegador=True) or {}
    saida, vistos = [], set()
    for a in (d.get('hoje') or []) + (d.get('futuro') or []):
        codigo = a.get('codigo') or f"inmet-{a.get('id')}"
        if codigo in vistos or str(a.get('encerrado')).lower() == 'true':
            continue
        vistos.add(codigo)
        saida.append({'codigo': codigo, 'descricao': (a.get('descricao') or '').strip(), 'severidade': (a.get('severidade') or '').strip(),
                      'cor': a.get('aviso_cor') or '', 'inicio': _data_inmet(a.get('data_inicio'), a.get('hora_inicio')),
                      'fim': _data_inmet(a.get('data_fim'), a.get('hora_fim')), 'riscos': _lista_texto(a.get('riscos')),
                      'instrucoes': _lista_texto(a.get('instrucoes')),
                      'geocodes': [g for g in re.split(r'[,\s]+', a.get('geocodes') or '') if re.fullmatch(r'\d{7}', g)]})
    return saida


# ------------------------------------------------------------------ tempo
def centroide(ibge):
    d = _json(f'https://servicodados.ibge.gov.br/api/v3/malhas/municipios/{ibge}/metadados', timeout=20) or []
    c = (d[0] if d else {}).get('centroide') or {}
    return (c.get('latitude'), c.get('longitude')) if c.get('latitude') else (None, None)


VENTO = {'Fracos': 'fraco', 'Moderados': 'moderado', 'Fortes': 'forte', 'Muito Fortes': 'muito forte'}


def tempo(ibge, lat=None, lon=None):
    """{previsao: [{dia, periodos:{manha,tarde,noite}:{resumo, vento, dir_vento}, temp_max, temp_min, umid_max, umid_min}],
        agora: {...}, proximas: {chuva_mm, chuva_prob, rajada_max, rajada_hora, chuva_hora}, lat, lon}"""
    saida = {'previsao': [], 'agora': None, 'proximas': None, 'lat': lat, 'lon': lon}
    try:
        p = (_json(f'https://apiprevmet3.inmet.gov.br/previsao/{ibge}', timeout=30, navegador=True) or {}).get(str(ibge)) or {}
        for dia, v in list(p.items())[:5]:
            per = {k: v[k] for k in ('manha', 'tarde', 'noite') if isinstance(v.get(k), dict)} or ({'dia': v} if isinstance(v, dict) and v.get('resumo') else {})
            base = next(iter(per.values()), {}) if per else {}
            saida['previsao'].append({
                'dia': dia,
                'periodos': {k: {'resumo': x.get('resumo'), 'vento': VENTO.get(x.get('int_vento'), (x.get('int_vento') or '').lower()), 'dir_vento': x.get('dir_vento')} for k, x in per.items()},
                'temp_max': base.get('temp_max'), 'temp_min': base.get('temp_min'), 'umid_max': base.get('umidade_max'), 'umid_min': base.get('umidade_min')})
    except Exception:  # noqa: BLE001
        pass
    if lat is None:
        try:
            lat, lon = centroide(ibge)
            saida['lat'], saida['lon'] = lat, lon
        except Exception:  # noqa: BLE001
            pass
    if lat is not None:
        try:
            o = _json('https://api.open-meteo.com/v1/forecast?' + urllib.parse.urlencode({
                'latitude': lat, 'longitude': lon, 'timezone': 'America/Sao_Paulo', 'forecast_hours': 24, 'wind_speed_unit': 'kmh',
                'current': 'temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,wind_speed_10m,wind_gusts_10m,weather_code,uv_index',
                'hourly': 'precipitation,precipitation_probability,wind_gusts_10m'}), timeout=20)
            c = o.get('current') or {}
            saida['agora'] = {'hora': c.get('time'), 'temperatura': c.get('temperature_2m'), 'sensacao': c.get('apparent_temperature'),
                              'umidade': c.get('relative_humidity_2m'), 'chuva_mm': c.get('precipitation'), 'vento': c.get('wind_speed_10m'),
                              'rajada': c.get('wind_gusts_10m'), 'uv': c.get('uv_index'), 'codigo': c.get('weather_code')}
            h = o.get('hourly') or {}
            horas, chuva, prob, raj = h.get('time') or [], h.get('precipitation') or [], h.get('precipitation_probability') or [], h.get('wind_gusts_10m') or []
            if horas:
                i_raj = max(range(len(raj)), key=lambda i: raj[i] or 0) if raj else None
                i_chu = max(range(len(chuva)), key=lambda i: chuva[i] or 0) if chuva else None
                saida['proximas'] = {'chuva_mm': round(sum(x or 0 for x in chuva), 1), 'chuva_prob': max((x or 0) for x in prob) if prob else None,
                                     'chuva_max_hora_mm': chuva[i_chu] if i_chu is not None else None, 'chuva_hora': horas[i_chu] if i_chu is not None and chuva[i_chu] else None,
                                     'rajada_max': raj[i_raj] if i_raj is not None else None, 'rajada_hora': horas[i_raj] if i_raj is not None else None}
        except Exception:  # noqa: BLE001
            pass
    return saida


# ------------------------------------------------------------------ publicações
# "Saúde da população": o que passa por este filtro (título + resumo). Agenda de ministro, obra e cerimônia ficam de fora.
TEMAS = [
    ('doencas-transmissiveis', r'dengue|chikungunya|zika|arbovir|gripe|influenza|srag|covid|sarampo|coqueluche|febre amarela|mpox|tuberculose|hanseni|hiv|aids|hepatite|malaria|mal[áa]ria|oropouche|surto|outbreak|epidemi|measles|cholera|c[óo]lera|polio|p[óo]lio|infec'),
    ('vacinacao', r'vacin|imuniza|vaccin|immuniz'),
    ('cronicas', r'diabet|emagrec|semaglut|tirzepat|glp-1|hipertens|hypertens|obesi|c[âa]ncer|cancer|cardio|avc|stroke|doen[çc]as? cr[ôo]nic|noncommunicable|\bncds?\b|dcnt|renal|asma|dpoc'),
    ('alimentacao', r'aliment|nutri|fome|inseguran[çc]a alimentar|ultraprocess|a[çc][úu]car|sugar|\bsal\b|salt|food|diet|amamenta|breastfeed|obesity|obesidade'),
    ('saude-mental', r'sa[úu]de mental|mental health|depress|ansiedade|anxiety'),
    ('clima-ambiente', r'calor|heat|clima|climate|polui|pollution|queimada|fuma[çc]a|enchente|flood|[áa]gua pot|saneamento|sanitation|water'),
    ('habitos', r'tabag|tobacco|fumo|cigarro|[áa]lcool|alcohol|atividade f[íi]sica|physical activity|sedentar'),
    ('vigilancia', r'boletim epidemiol|vigil[âa]ncia|surveillance|mortalidade|mortality|expectativa de vida|life expectancy|relat[óo]rio|report|guideline|diretriz'),
]
_RE_TEMAS = [(t, re.compile(p, re.I)) for t, p in TEMAS]


def tema(texto):
    for t, r in _RE_TEMAS:
        if r.search(texto or ''):
            return t
    return None


def _limpo(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()


def _feed(url, fonte, idioma, navegador=False):
    """RSS 2.0 ou Atom → [{url, fonte, titulo, resumo, publicado_em, idioma}]"""
    t = _get(url, timeout=30, navegador=navegador).decode('utf-8', 'ignore')
    itens = []
    for bloco in re.findall(r'<(?:item|entry)\b.*?</(?:item|entry)>', t, re.S):
        tit = re.search(r'<title[^>]*>(.*?)</title>', bloco, re.S)
        link = re.search(r'<link[^>]*href="([^"]+)"', bloco) or re.search(r'<link>(.*?)</link>', bloco, re.S)
        res = re.search(r'<(?:description|summary|content)[^>]*>(.*?)</(?:description|summary|content)>', bloco, re.S)
        data = re.search(r'<(?:pubDate|published|updated|dc:date)>(.*?)<', bloco)
        titulo = _limpo(re.sub(r'<!\[CDATA\[|\]\]>', '', tit.group(1) if tit else ''))
        href = _limpo(link.group(1) if link else '')
        if not titulo or not href:
            continue
        itens.append({'url': href, 'fonte': fonte, 'titulo': titulo[:300], 'resumo': _limpo(re.sub(r'<!\[CDATA\[|\]\]>', '', res.group(1) if res else ''))[:500],
                      'publicado_em': _quando(data.group(1) if data else ''), 'idioma': idioma})
    return itens


def _quando(s):
    s = (s or '').strip()
    for f in ('%a, %d %b %Y %H:%M:%S %z', '%a, %d %b %Y %H:%M:%S %Z', '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S.%f%z'):
        try:
            d = dt.datetime.strptime(s, f)
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None


def _ms_noticias():
    """A página de notícias do Ministério não tem RSS que funcione: lê a listagem (15 mais recentes)."""
    t = _get('https://www.gov.br/saude/pt-br/assuntos/noticias', timeout=30, navegador=True).decode('utf-8', 'ignore')
    itens = []
    for bloco in re.findall(r'<article class="tileItem.*?</article>', t, re.S):
        a = re.search(r'class="summary url" href="([^"]+)"[^>]*>(.*?)</a>', bloco, re.S)
        if not a:
            continue
        desc = re.search(r'class="description">(.*?)</span>', bloco, re.S)
        pub = re.search(r'icon-day"></i>\s*(\d{2})/(\d{2})/(\d{4}).*?icon-hour"></i>\s*(\d{2})h(\d{2})', bloco, re.S)
        quando = dt.datetime(int(pub[3]), int(pub[2]), int(pub[1]), int(pub[4]), int(pub[5]), tzinfo=BRT) if pub else None
        itens.append({'url': a.group(1), 'fonte': 'Ministério da Saúde', 'titulo': _limpo(a.group(2))[:300], 'resumo': _limpo(desc.group(1) if desc else '')[:500],
                      'publicado_em': quando, 'idioma': 'pt'})
    return itens


def _oms():
    """O RSS de notícias da OMS parou em fev/2026: a API do próprio site (notícias + surtos, Disease Outbreak News)."""
    itens = []
    for caminho, base, tema_fixo in (('newsitems', 'https://www.who.int/news/item', None),
                                     ('diseaseoutbreaknews', 'https://www.who.int/emergencies/disease-outbreak-news/item', 'doencas-transmissiveis')):
        d = _json(f'https://www.who.int/api/news/{caminho}?$orderby=PublicationDateAndTime%20desc&$top=' + ('20' if not tema_fixo else '6'), timeout=30, navegador=True) or {}
        for x in d.get('value') or []:
            titulo = _limpo(x.get('OverrideTitle') if x.get('UseOverrideTitle') else x.get('Title'))
            if not titulo or not x.get('ItemDefaultUrl'):
                continue
            itens.append({'url': base + x['ItemDefaultUrl'], 'fonte': 'OMS' if not tema_fixo else 'OMS · Surtos', 'titulo': titulo[:300],
                          'resumo': _limpo(x.get('Summary'))[:500], 'publicado_em': _quando(x.get('PublicationDateAndTime') or ''), 'idioma': 'en', 'tema_fixo': tema_fixo})
    return itens


def idioma(t):
    t = ' ' + sem_acento(t) + ' '
    if re.search(r' (da|do|das|dos|para|com|em|sao|nao) |cao |coes ', t):
        return 'pt'
    if re.search(r' (el|los|las|y|del|para|con) |cion |ciones ', t):
        return 'es'
    return 'en'


def publicacoes():
    """Junta as fontes, marca o tema e devolve só o que é saúde da população. Uma fonte fora do ar não derruba as outras."""
    ano = dt.date.today().year
    fontes = [
        (_ms_noticias, ()),
        (_feed, (f'https://www.gov.br/saude/pt-br/centrais-de-conteudo/publicacoes/boletins/epidemiologicos/edicoes/{ano}/RSS', 'Ministério da Saúde · Boletim Epidemiológico', 'pt', True)),
        (_feed, ('https://www.paho.org/pt/rss.xml', 'OPAS', 'pt')),
        (_oms, ()),
    ]
    saida, erros = [], []
    for f, args in fontes:
        try:
            for it in f(*args):
                if 'boletins/epidemiologicos' in it['url']:
                    if 'boletim' not in it['titulo'].lower():   # a pasta do ano também vem no feed
                        continue
                    it['tema'] = 'vigilancia'
                    it['url'] = re.sub(r'/view$', '', it['url'])
                else:   # só o título decide: o resumo da OMS menciona "health" em tudo
                    it['tema'] = it.pop('tema_fixo', None) or tema(it['titulo'])
                if it['fonte'] == 'OPAS':
                    it['idioma'] = idioma(it['titulo'])
                if it['tema']:
                    saida.append(it)
        except Exception as e:  # noqa: BLE001
            erros.append(f'{args[1] if len(args) > 1 else f.__name__}: {type(e).__name__}')
    return saida, erros


def sem_acento(s):
    return unicodedata.normalize('NFD', s or '').encode('ascii', 'ignore').decode().lower()


# ------------------------------------------------------------------ Ministério da Saúde · API de Dados Abertos (02/10/2026)
# Microdados (uma linha por notificação, amostra ou unidade): aqui só se conta. Nada pessoal sai daqui.
MS = 'https://apidadosabertos.saude.gov.br'


def _ms(caminho, **params):
    d = _json(MS + caminho + '?' + urllib.parse.urlencode(params), timeout=60)
    return next(iter(d.values()), []) if isinstance(d, dict) else []


def _paginas(caminho, tamanho=1000, maximo=30, **params):
    """A API corta a página em silêncio (o CNES devolve 20 mesmo pedindo 1000): pede até vir menos que o tamanho real."""
    saida, real = [], None
    for i in range(maximo):
        v = _ms(caminho, limit=tamanho, offset=len(saida), **params)
        saida += v
        real = real or len(v)
        if not v or len(v) < real:
            break
    return saida


def _dec(s):
    try:
        return float(str(s).replace(',', '.'))
    except (TypeError, ValueError):
        return None


def sisagua(ibge, uf=None, meses=3):
    """Vigilância da qualidade da água: os últimos `meses` com amostras, contados por mês.
    Rede pública (SAA) separada de soluções alternativas e poços (SAC/SAI), onde bactéria é mais comum e não diz nada da rede."""
    hoje, saida = dt.date.today(), []
    ano, mes = hoje.year, hoje.month
    for _ in range(8):
        linhas = _paginas('/sisagua/vigilancia-parametros-basicos', codigo_ibge=ibge[:6], ano=ano, mes=mes)
        if linhas:
            m = {'ano': ano, 'mes': mes, 'rede': {}, 'outras': {}}
            for x in linhas:
                g = m['rede' if x.get('tipo_da_forma_de_abastecimento') == 'SAA' else 'outras']
                p, r = x.get('parametro') or '', (x.get('resultado') or '').upper()
                chave = {'Escherichia coli': 'ecoli', 'Coliformes totais': 'coliformes'}.get(p) or ('turbidez' if p.startswith('Turbidez') else None)
                if not chave:
                    continue
                c = g.setdefault(chave, {'analises': 0, 'fora': 0})
                c['analises'] += 1
                if chave in ('ecoli', 'coliformes') and r == 'PRESENTE':
                    c['fora'] += 1
                elif chave == 'turbidez' and (_dec(r) or 0) > 5:   # padrão na distribuição: até 5 uT (Portaria GM/MS 888/2021)
                    c['fora'] += 1
            m['amostras'] = len({x.get('numero_da_amostra') for x in linhas})
            m['ultima_coleta'] = max((x.get('data_da_coleta') or '') for x in linhas) or None
            saida.append(m)
            if len(saida) == meses:
                break
        ano, mes = (ano, mes - 1) if mes > 1 else (ano - 1, 12)
    return {'meses': saida} if saida else None


CLASSI_ZIKA = {'1': 'confirmados', '2': 'descartados', '8': 'inconclusivos'}


def zika(ibge, uf=None):
    """Notificações de zika registradas no município no ano (SINAN): confirmadas, descartadas e em investigação."""
    ano = dt.date.today().year
    d = zika_ano(ibge, ano)
    if not d['notificacoes'] and dt.date.today().month <= 2:   # começo do ano: o ano novo ainda não tem registro
        d = zika_ano(ibge, ano - 1)
    return d


def zika_ano(ibge, ano, nomes=None):
    v = _paginas('/arboviroses/zikavirus', nu_ano=int(ano), id_municip=ibge[:6])
    ano = int(ano)
    c = {'confirmados': 0, 'descartados': 0, 'inconclusivos': 0, 'em_investigacao': 0}
    for x in v:
        c[CLASSI_ZIKA.get(str(x.get('classi_fin')), 'em_investigacao')] += 1
    return {'ano': ano, 'notificacoes': len(v), **c, 'ultima_notificacao': max((x.get('dt_notific') or '' for x in v), default='') or None}


def periodo_febre_amarela(hoje=None):
    """O monitoramento da febre amarela vai de julho a junho (ex.: 2025/2026)."""
    hoje = hoje or dt.date.today()
    return f'{hoje.year}/{hoje.year + 1}' if hoje.month >= 7 else f'{hoje.year - 1}/{hoje.year}'


_FA = {'em': 0, 'humanos': [], 'epizootias': []}


def _febre_amarela_brasil():
    """Base nacional (poucos milhares de linhas, sem filtro por município na API): uma vez por dia por processo."""
    if time.time() - _FA['em'] > 86400:
        ano = dt.date.today().year
        _FA['humanos'] = _paginas('/arboviroses/febre-amarela-humanos-primatas-nao-humanos')
        _FA['epizootias'] = [x for a in (ano - 1, ano) for x in _paginas('/arboviroses/febre-amarela-epzootias', ano_ocor=a)]
        _FA['em'] = time.time()
    return _FA


def febre_amarela(ibge, uf=None):
    """Casos humanos (e óbitos) e epizootias (macacos mortos) no município, na UF e no país: o período de monitoramento
    corrente e o anterior (o corrente começa em julho quase zerado; a sazonalidade é de dezembro a maio)."""
    b, hoje, mun = _febre_amarela_brasil(), dt.date.today(), ibge[:6]
    def conta(h, e):
        return {'casos': len(h), 'obitos': sum(1 for x in h if str(x.get('obito')).upper() == 'SIM'), 'epizootias': len(e)}
    periodos = []
    for per in (periodo_febre_amarela(hoje), periodo_febre_amarela(hoje.replace(year=hoje.year - 1))):
        hum = [x for x in b['humanos'] if x.get('monitoramento_is') == per]
        epi = [x for x in b['epizootias'] if x.get('monitoramento_ocor') == per]
        periodos.append({'periodo': per,
                         'municipio': conta([x for x in hum if str(x.get('cod_mun_lpi')) == mun], [x for x in epi if str(x.get('cod_mun_ocor')) == mun]),
                         'estado': conta([x for x in hum if x.get('uf_lpi') == uf], [x for x in epi if x.get('uf_ocor') == uf]) if uf else None,
                         'brasil': conta(hum, epi)})
    return {'uf': uf, 'periodos': periodos}


TIPO_UBS = {1: 'Posto de saúde', 2: 'Centro de saúde / UBS'}
_PRISIONAL = re.compile(r'detencao|penitenci|prisional|presidio|cadeia|fundacao casa|socioeducativ|\bcdp\b|\bcpp\b')   # postos sem acesso do público


def ubs(ibge, uf=None):
    """Unidades básicas ativas no CNES (posto de saúde e centro de saúde/UBS), com endereço e telefone públicos."""
    lista = []
    for tipo in TIPO_UBS:
        for x in _paginas('/cnes/estabelecimentos', tamanho=20, maximo=60, codigo_municipio=ibge[:6], codigo_tipo_unidade=tipo, status=1):
            if x.get('estabelecimento_faz_atendimento_ambulatorial_sus') != 'SIM' or _PRISIONAL.search(sem_acento(x.get('nome_fantasia'))):
                continue
            end = ' '.join(s for s in (x.get('endereco_estabelecimento'), x.get('numero_estabelecimento')) if s)
            lista.append({'cnes': x.get('codigo_cnes'), 'nome': (x.get('nome_fantasia') or '').strip().title(), 'tipo': TIPO_UBS[tipo],
                          'endereco': end.title(), 'bairro': (x.get('bairro_estabelecimento') or '').title(), 'cep': x.get('codigo_cep_estabelecimento'),
                          'telefone': x.get('numero_telefone_estabelecimento'), 'turno': (x.get('descricao_turno_atendimento') or '').capitalize(),
                          'lat': x.get('latitude_estabelecimento_decimo_grau'), 'lon': x.get('longitude_estabelecimento_decimo_grau')})
    lista.sort(key=lambda u: sem_acento(u['nome']))
    return {'total': len(lista), 'unidades': lista}


# Previne Brasil: códigos 10…70 da API = indicadores 1…7 (numerador ÷ denominador = percentual_quadrimestre); metas da Portaria GM/MS 102/2022
PREVINE = {10: ('Gestantes com 6 ou mais consultas de pré-natal, a 1ª até a 12ª semana', 45),
           20: ('Gestantes com exames de sífilis e HIV', 60),
           30: ('Gestantes com atendimento odontológico', 60),
           40: ('Mulheres de 25 a 64 anos com exame preventivo (citopatológico)', 40),
           50: ('Crianças de 1 ano com vacinas pólio e pentavalente', 95),
           60: ('Pessoas com hipertensão com pressão aferida no semestre', 50),
           70: ('Pessoas com diabetes com hemoglobina glicada solicitada', 50)}


def variacao(pontos, chave):
    """Primeiro × último ponto de uma série [{chave, percentual}]: pontos percentuais e variação relativa."""
    p = [x for x in pontos if x.get('percentual') is not None]
    if len(p) < 2:
        return None
    a0, a1 = p[0], p[-1]
    return {'de': a0[chave], 'ate': a1[chave], 'de_pct': a0['percentual'], 'ate_pct': a1['percentual'],
            'pontos': round(a1['percentual'] - a0['percentual'], 2),
            'variacao_pct': round((a1['percentual'] / a0['percentual'] - 1) * 100, 1) if a0['percentual'] else None}


def previne(ibge, uf=None):
    """Último quadrimestre e a série inteira que a API guarda (2022–2024), por indicador."""
    v = [x for x in _paginas('/atencao-primaria/indicador-desempenho-programa-previne-brasil', codigo_municipio=ibge[:6]) if x.get('visao_equipe') == 'geral']
    if not v:
        return None
    q = max(x.get('quadrimestre') or '' for x in v)
    serie = {}
    for x in sorted(v, key=lambda x: x.get('quadrimestre') or ''):
        if x.get('codigo_tipo_indicador') in PREVINE:
            serie.setdefault(x['codigo_tipo_indicador'], []).append({'quadrimestre': x.get('quadrimestre'), 'percentual': x.get('percentual_quadrimestre')})
    ind = [{'codigo': x['codigo_tipo_indicador'], 'nome': PREVINE[x['codigo_tipo_indicador']][0], 'meta': PREVINE[x['codigo_tipo_indicador']][1],
            'percentual': x.get('percentual_quadrimestre'), 'numerador': x.get('numerador'), 'denominador': x.get('denominador_utilizador'),
            'serie': serie.get(x['codigo_tipo_indicador'], []), 'tendencia': variacao(serie.get(x['codigo_tipo_indicador'], []), 'quadrimestre')}
           for x in v if x.get('quadrimestre') == q and x.get('codigo_tipo_indicador') in PREVINE]
    return {'quadrimestre': q, 'indicadores': sorted(ind, key=lambda i: i['codigo'])}


_REGIAO = {}
_MINUSCULAS = {'de', 'da', 'do', 'das', 'dos', 'e', 'd'}


def _titulo(s):
    p = (s or '').lower().title().split(' ')
    return ' '.join(w.lower() if i and w.lower() in _MINUSCULAS else w for i, w in enumerate(p)).replace("D'O", "d'O")
_PREVINE_UF = {}


def regiao_de_saude(ibge):
    """Região e macrorregião de saúde (CIR) do município e as cidades que fazem parte dela."""
    m = _ms('/macrorregiao-e-regiao-de-saude/municipio', codigo_municipio=ibge[:6], limit=5)
    if not m:
        return None
    cod = m[0].get('codigo_regiao_saude')
    if cod not in _REGIAO:
        _REGIAO[cod] = _ms('/macrorregiao-e-regiao-de-saude/municipio', codigo_regiao_saude=cod, limit=500)
    def nome(s):
        return _titulo(re.sub(r'^[A-Z]{2} - ', '', s or ''))
    return {'codigo': cod, 'nome': _titulo(m[0].get('regiao_saude')), 'macro': m[0].get('macrorregiao_saude'),
            'municipios': [{'ibge': str(x.get('codigo_municipio')), 'nome': nome(x.get('municipio')), 'populacao': x.get('populacao_estimada_ibge_2022')}
                           for x in _REGIAO[cod]]}


def _previne_uf(uf, q):
    """Todos os municípios da UF num quadrimestre (visão geral): {ibge6: {código: (numerador, denominador, %)}}. Uma vez por semana por processo."""
    k = (uf, q)
    if k not in _PREVINE_UF or time.time() - _PREVINE_UF[k][0] > 7 * 86400:
        t = {}
        for x in _paginas('/atencao-primaria/indicador-desempenho-programa-previne-brasil', maximo=40, uf=uf, quadrimestre=q):
            if x.get('visao_equipe') == 'geral' and x.get('codigo_tipo_indicador') in PREVINE:
                t.setdefault(str(x.get('codigo_municipio')), {})[x['codigo_tipo_indicador']] = (x.get('numerador') or 0, x.get('denominador_utilizador') or 0, x.get('percentual_quadrimestre'))
        _PREVINE_UF[k] = (time.time(), t)
    return _PREVINE_UF[k][1]


def comparacao(ibge, uf=None):
    """O município ao lado da sua região de saúde e do estado, nos indicadores do Previne (mesmo quadrimestre)."""
    r = regiao_de_saude(ibge)
    p = previne(ibge, uf)
    if not r or not p or not uf:
        return None
    tab = _previne_uf(uf, p['quadrimestre'])
    if not tab:
        return None
    membros = {m['ibge']: m['nome'] for m in r['municipios']}
    def taxa(linhas):
        num = sum(x[0] for x in linhas)
        den = sum(x[1] for x in linhas)
        return round(num / den * 100, 1) if den else None
    ind = []
    for i in p['indicadores']:
        c = i['codigo']
        reg = [(m, tab[m][c]) for m in membros if c in tab.get(m, {})]
        ord_ = sorted((x for x in reg if x[1][2] is not None), key=lambda x: -x[1][2])
        pos = next((k + 1 for k, x in enumerate(ord_) if x[0] == ibge[:6]), None)
        ind.append({'codigo': c, 'nome': i['nome'], 'meta': i['meta'], 'municipio': i['percentual'],
                    'regiao': taxa([x[1] for x in reg]), 'estado': taxa([v[c] for v in tab.values() if c in v]),
                    'posicao': pos, 'de': len(ord_),
                    'melhor': {'nome': membros[ord_[0][0]], 'percentual': ord_[0][1][2]} if ord_ else None})
    return {'quadrimestre': p['quadrimestre'], 'uf': uf, 'municipios_uf': len(tab),
            'regiao': {k: r[k] for k in ('codigo', 'nome', 'macro')} | {'municipios': len(membros)}, 'indicadores': ind}


# SISVAN: o relatório público (formulário POST, ~7 s por consulta). A API de Dados Abertos só tem registros antigos.
SISVAN_FASES = (('adulto', '3', []), ('idoso', '4', []), ('gestante', '5', [('nu_idade_ges', '99')]),
                ('crianca', '1', [('nu_idade_inicio', '0'), ('nu_idade_fim', '5'), ('nu_indice_cri', '2')]))   # criança: 0 a 5 anos, peso × altura


def _int_br(s):
    try:
        return int(str(s).replace('.', ''))
    except ValueError:
        return None


def _tabela_sisvan(s):
    t = next((x for x in re.findall(r'<table.*?</table>', s, re.S) if 'Quantidade' in x), None)   # só a de adultos tem id="relatorio"
    if not t:
        return None
    classes = [_limpo(c) for c in re.findall(r'<th[^>]*colspan="2"[^>]*>(.*?)</th>', t, re.S)]
    corpo = re.search(r'<tbody>(.*?)</tbody>', t, re.S)
    saida = {'classes': classes}
    for tr in re.findall(r'<tr.*?</tr>', corpo.group(1) if corpo else '', re.S):
        cel = [_limpo(c) for c in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]
        i = next((k for k, c in enumerate(cel) if c.startswith('TOTAL')), None)
        rot, nums = ('municipio', cel[5:]) if i is None else ('estado' if 'ESTADO' in cel[i] else 'brasil' if 'BRASIL' in cel[i] else None, cel[i + 1:])
        if not rot or len(nums) != 2 * len(classes) + 1:
            continue
        saida[rot] = {'total': _int_br(nums[-1]), 'quantidade': [_int_br(nums[2 * k]) for k in range(len(classes))],
                      'percentual': [_dec(nums[2 * k + 1].rstrip('%')) for k in range(len(classes))]}
    return saida if 'municipio' in saida else None


SISVAN_HIST = ('adulto', 'crianca')   # fases da série de 5 anos (sobrepeso e obesidade)
SISVAN_ANOS = 5
SISVAN_BASE = 'https://sisaps.saude.gov.br/sisvan/relatoriopublico/'


def _sisvan_op():
    import http.cookiejar
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    op.addheaders = [('User-Agent', 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36')]
    op.open(SISVAN_BASE + 'index', timeout=60).read()
    return op


def _sisvan_ano(op, ibge, ano, fases):
    saida = {}
    for nome, fase, extra in fases:
        dados = [('tpRelatorio', '2'), ('coVisualizacao', '1'), ('nuAno', str(ano)), ('nuMes[]', '99'), ('tpFiltro', 'M'),
                 ('coRegiao', ibge[0]), ('coUfIbge', ibge[:2]), ('coMunicipioIbge', ibge[:6]), ('noRegional', '99'),
                 ('st_cobertura', '99'), ('nu_ciclo_vida', fase), ('ds_sexo2', '1'), ('ds_raca_cor2', '99'),
                 ('co_sistema_origem', '0'), ('CO_POVO_COMUNIDADE', 'TODOS'), ('CO_ESCOLARIDADE', 'TODOS'), ('verTela', '')] + extra
        try:
            s = op.open(SISVAN_BASE + 'estadonutricional', urllib.parse.urlencode(dados).encode(), timeout=120).read().decode('utf-8', 'ignore')
            t = _tabela_sisvan(s)
            if t:
                saida[nome] = t
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return {'ano': int(ano), 'fases': saida} if saida else None


def sisvan(ibge, uf=None):
    """Último ano fechado, as quatro fases — o que a página mostra na hora."""
    return _sisvan_ano(_sisvan_op(), ibge, dt.date.today().year - 1, SISVAN_FASES)


def sisvan_ano(ibge, ano, nomes=SISVAN_HIST):
    """Um ano do relatório público (adulto e criança, para a série histórica)."""
    fases = [x for x in SISVAN_FASES if x[0] in nomes]
    return _sisvan_ano(_sisvan_op(), ibge, int(ano), fases) if fases else None


def anos_sisvan(hoje=None):
    y = (hoje or dt.date.today()).year - 1
    return list(range(y - (SISVAN_ANOS - 1), y + 1))


def anos_zika(hoje=None):
    y = (hoje or dt.date.today()).year - 1
    return list(range(y - 3, y + 1))


# fontes com série anual preenchida aos poucos (um ano faltante por vez): fonte → (anos, busca de um ano)
HISTORICO = {'sisvan': (anos_sisvan, sisvan_ano), 'zika': (anos_zika, zika_ano)}


def serie_zika(por_ano):
    """Snapshots {ano: zika} → notificações e confirmados por ano, do mais antigo ao mais novo."""
    return [{'ano': int(a), 'notificacoes': d.get('notificacoes', 0), 'confirmados': d.get('confirmados', 0)}
            for a, d in sorted(por_ano.items(), key=lambda x: int(x[0])) if d]


def serie_agua(meses):
    """Meses do SISAGUA (de vários snapshots) → % de análises da rede com E. coli, mês a mês (12 mais recentes)."""
    por = {}
    for m in meses:
        e = ((m or {}).get('rede') or {}).get('ecoli') or {}
        if m.get('ano') and m.get('mes') and e.get('analises'):
            por[(int(m['ano']), int(m['mes']))] = {'mes': f"{int(m['ano'])}-{int(m['mes']):02d}", 'analises': e['analises'],
                                                   'percentual': round(e.get('fora', 0) / e['analises'] * 100, 1)}
    return [por[k] for k in sorted(por)][-12:]


def periodo_de(fonte, dados):
    """Chave do snapshot: o período que o dado descreve (ano, mês, quadrimestre), não a hora da coleta."""
    if not dados:
        return None
    if fonte in ('sisvan', 'zika', 'lavoura'):
        a = dados.get('ano')
        return str(a) if a else None
    if fonte in ('previne', 'comparacao'):
        q = dados.get('quadrimestre')
        return str(q) if q else None
    if fonte == 'ibge_alimentacao':
        a = (dados.get('seguranca') or {}).get('ano')
        return str(a) if a else None
    if fonte == 'sisagua':
        m = (dados.get('meses') or [None])[0]
        return f"{m['ano']}-{int(m['mes']):02d}" if m and m.get('ano') and m.get('mes') else None
    if fonte == 'perfil_ibge':
        e = (dados.get('estimativa') or {}).get('ano')
        return f'estimativa-{e}' if e else ('censo-2022' if dados.get('censo') else None)
    if fonte == 'febre_amarela':
        return (dados.get('periodos') or [{}])[0].get('periodo')
    return None


def _tem_classe(nome, *pals):
    n = sem_acento(nome)
    return any(p in n for p in pals)


def _pct_fase(fase, pred):
    if not fase:
        return None
    vals = [p for c, p in zip(fase.get('classes') or [], ((fase.get('municipio') or {}).get('percentual') or []))
            if pred(c) and p is not None]
    return round(sum(vals), 2) if vals else None


# métricas cuja série o observatório persiste e mostra (SISVAN, município)
SISVAN_METRICAS = (
    ('adulto_excesso', 'adulto', 'Sobrepeso e obesidade em adultos',
     lambda c: _tem_classe(c, 'sobrepeso', 'obesidade')),
    ('adulto_obesidade', 'adulto', 'Obesidade em adultos',
     lambda c: _tem_classe(c, 'obesidade')),
    ('adulto_sobrepeso', 'adulto', 'Sobrepeso em adultos',
     lambda c: _tem_classe(c, 'sobrepeso') and not _tem_classe(c, 'obesidade')),
    ('crianca_excesso', 'crianca', 'Risco de sobrepeso, sobrepeso e obesidade em crianças de 0 a 5 anos',
     lambda c: _tem_classe(c, 'sobrepeso', 'obesidade')),
    ('crianca_obesidade', 'crianca', 'Obesidade em crianças de 0 a 5 anos',
     lambda c: _tem_classe(c, 'obesidade')),
    ('idoso_excesso', 'idoso', 'Sobrepeso e obesidade em pessoas idosas',
     lambda c: _tem_classe(c, 'sobrepeso', 'obesidade')),
)


def tendencias_sisvan(por_ano):
    """Snapshots {ano: {fases}} → (serie, tendencias). Só fala em alta/queda quando há pelo menos dois anos."""
    serie, tendencias = {}, []
    anos = sorted({int(a) for a in por_ano if str(a).isdigit()})
    for mid, fase, nome, pred in SISVAN_METRICAS:
        pontos = []
        for a in anos:
            d = por_ano.get(a) or por_ano.get(str(a)) or {}
            v = _pct_fase((d.get('fases') or {}).get(fase), pred)
            if v is not None:
                pontos.append({'ano': a, 'percentual': v})
        if not pontos:
            continue
        serie[mid] = {'nome': nome, 'fase': fase, 'pontos': pontos}
        if len(pontos) < 2:
            continue
        a0, a1 = pontos[0], pontos[-1]
        janela = a1['ano'] - a0['ano']
        if janela < 1:
            continue
        ant = next((p for p in reversed(pontos[:-1]) if a1['ano'] - p['ano'] == 1), None)
        pp = round(a1['percentual'] - a0['percentual'], 2)
        var = None if not a0['percentual'] else round((a1['percentual'] / a0['percentual'] - 1) * 100, 1)
        t = {'id': mid, 'nome': nome, 'fase': fase, 'de': a0['ano'], 'ate': a1['ano'], 'anos': janela,
             'de_pct': a0['percentual'], 'ate_pct': a1['percentual'], 'pontos': pp, 'variacao_pct': var}
        if ant:
            t['ano_anterior_pct'] = ant['percentual']
            t['pontos_ano'] = round(a1['percentual'] - ant['percentual'], 2)
        tendencias.append(t)
    return serie, tendencias


# POF 2017-2018: kg/pessoa/ano adquiridos para consumo no domicílio (não inclui refeição fora de casa). Grupos de 1º nível.
POF_GRUPOS = ((3865, 'Cereais e leguminosas'), (3901, 'Hortaliças'), (3981, 'Frutas'), (4076, 'Farinhas, féculas e massas'),
              (4119, 'Panificados'), (4158, 'Carnes'), (4259, 'Pescados'), (4466, 'Aves e ovos'), (4504, 'Laticínios'),
              (104448, 'Açúcares e doces'), (4676, 'Óleos e gorduras'), (4698, 'Bebidas e infusões'),
              (104472, 'Alimentos preparados e misturas industriais'))
SEG_CATS = {'109098': 'segura', '109099': 'insegura', '109100': 'leve', '109101': 'moderada', '109102': 'grave'}


def _ibge(caminho):
    return _json('https://servicodados.ibge.gov.br/api/v3/agregados/' + caminho)


def _ibge_vars(caminho):
    """{id_da_variável: (ano, valor)} da primeira série de cada bloco."""
    saida = {}
    try:
        blocos = _ibge(caminho) or []
    except Exception:  # noqa: BLE001
        return saida
    for b in blocos:
        serie = ((b.get('resultados') or [{}])[0].get('series') or [{}])[0].get('serie') or {}
        if not serie:
            continue
        ano = max(serie)
        saida[str(b.get('id'))] = (ano, _dec(serie.get(ano)))
    return saida


def _ibge_cats(caminho, classe=None):
    """[(categoria, ano, valor), ...] da primeira variável. `classe` escolhe a classificação (idade, sexo…)."""
    try:
        blocos = _ibge(caminho) or []
    except Exception:  # noqa: BLE001
        return []
    saida, classe = [], None if classe is None else str(classe)
    for res in (blocos[0].get('resultados') or []) if blocos else []:
        cat = None
        for c in res.get('classificacoes') or []:
            if classe and str(c.get('id')) != classe:
                continue
            cat = next(iter(c.get('categoria') or {}), None)
            if classe:
                break
        serie = ((res.get('series') or [{}])[0].get('serie') or {})
        if not serie or cat is None:
            continue
        ano = max(serie)
        saida.append((str(cat), ano, _dec(serie.get(ano))))
    return saida


def _inteiro(x):
    return None if x is None else int(round(x))


def _pct(parte, total, casas=1):
    if parte is None or not total:
        return None
    return round(100 * parte / total, casas)


def _serie_ibge(blocos, nome_local):
    """{categoria: valor} da série da localidade (Brasil ou UF) no primeiro (e único) período."""
    saida = {}
    for res in (blocos[0].get('resultados') or []) if blocos else []:
        cat = next(iter((res.get('classificacoes') or [{}])[-1].get('categoria') or {}), None)
        for s in res.get('series') or []:
            if s.get('localidade', {}).get('nome') != nome_local:
                continue
            serie = s.get('serie') or {}
            v = serie.get(max(serie)) if serie else None
            saida[str(cat)] = _dec(v)
    return saida


def ibge_alimentacao(ibge, uf=None):
    """Segurança alimentar no estado (PNAD Contínua, último ano) e aquisição domiciliar de alimentos (POF 2018).
    A amostra das duas pesquisas não comporta município — o retrato é da UF e do Brasil."""
    cod = str(ibge)[:2]
    try:
        seg = _ibge('9552/periodos/2024/variaveis/9784?localidades=N1[all]|N3[' + cod + ']&classificacao=1[6795]|12404[' +
                    ','.join(SEG_CATS) + ']')
    except Exception:  # noqa: BLE001
        seg = []
    try:
        pof = _ibge('2393/periodos/2018/variaveis/1207?localidades=N1[all]|N3[' + cod + ']&classificacao=217[' +
                    ','.join(str(i) for i, _ in POF_GRUPOS) + ']')
    except Exception:  # noqa: BLE001
        pof = []
    nome_uf = next((s['localidade']['nome'] for b in (seg or pof or []) for r in b.get('resultados') or []
                    for s in r.get('series') or [] if s.get('localidade', {}).get('nivel', {}).get('id') == 'N3'), uf)
    def faixa(blocos, nome):
        bruto = _serie_ibge(blocos, nome)
        return {SEG_CATS[k]: bruto[k] for k in SEG_CATS if k in bruto} if bruto else None
    def grupos(blocos, nome):
        bruto = _serie_ibge(blocos, nome)
        return [{'nome': rotulo, 'kg': bruto.get(str(i))} for i, rotulo in POF_GRUPOS if str(i) in bruto]
    saida = {'uf': nome_uf or uf, 'codigo_uf': cod}
    if seg:
        saida['seguranca'] = {'ano': 2024, 'pesquisa': 'PNAD Contínua', 'estado': faixa(seg, nome_uf), 'brasil': faixa(seg, 'Brasil')}
    if pof:
        saida['pof'] = {'ano': 2018, 'unidade': 'kg por pessoa por ano', 'estado': grupos(pof, nome_uf), 'brasil': grupos(pof, 'Brasil')}
    return saida if saida.get('seguranca') or saida.get('pof') else None


_SAR = {'em': 0, 'linhas': []}


def _km(a, b, c, d):
    if None in (a, b, c, d):
        return None
    from math import asin, cos, radians, sin, sqrt
    p1, p2, dp, dl = radians(a), radians(c), radians(c - a), radians(d - b)
    return round(2 * 6371 * asin(min(1, sqrt(sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2))), 1)


def _sar_brasil():
    """Todos os reservatórios do SAR (SIN, Nordeste, Cantareira, DF, RMBH): uma leitura a cada 6 h por processo."""
    if time.time() - _SAR['em'] < 6 * 3600 and _SAR['linhas']:
        return _SAR['linhas']
    campos = '*'  # as camadas não compartilham todos os campos (VO_UTIL só no SIN)
    linhas = []
    for camada in range(5):
        try:
            d = _json('https://www.snirh.gov.br/arcgis/rest/services/IG/SAR/MapServer/' + str(camada) + '/query?' +
                      urllib.parse.urlencode({'where': '1=1', 'outFields': campos, 'returnGeometry': 'false', 'f': 'json',
                                             'resultRecordCount': 2000}), timeout=40)
        except Exception:  # noqa: BLE001
            continue
        for f in d.get('features') or []:
            a = f.get('attributes') or {}
            pct = _dec(a.get('SAR_PC_VOLUME'))
            if pct is None:
                pct = _dec(a.get('SAR_VO_UTIL_ULTIMA'))
            linhas.append({'nome': (a.get('SAR_NM') or '').strip(), 'municipio': (a.get('SAR_NM_MUNICIPIO') or '').strip(),
                           'uf': (a.get('SAR_SG_UF') or '').upper(), 'bacia': a.get('SAR_NM_BACIA') or a.get('CBE_NM'),
                           'sistema': a.get('SAR_TP_SISTEMA'), 'tipo': a.get('SAR_TP_RESERVATORIO'),
                           'capacidade_hm3': _dec(a.get('SAR_QT_CAPACIDADE')), 'volume_hm3': _dec(a.get('SAR_VO_ULTIMA')),
                           'percentual': None if pct is None else round(pct, 1), 'medicao': a.get('SAR_DT_ULTIMA_MEDICAO'),
                           'lat': a.get('SAR_NU_LATITUDE'), 'lon': a.get('SAR_NU_LONGITUDE')})
    _SAR['linhas'], _SAR['em'] = linhas, time.time()
    return linhas


def reservatorios(ibge, uf=None):
    """Reservatórios da ANA no município e os mais próximos (o SAR não cobre todo açude: só os monitorados)."""
    uf = (uf or '').upper()
    try:
        nome = ((_json(f'https://servicodados.ibge.gov.br/api/v1/localidades/municipios/{ibge}') or {}).get('nome') or '')
    except Exception:  # noqa: BLE001
        nome = ''
    try:
        lat, lon = centroide(ibge if len(str(ibge)) >= 7 else str(ibge))
    except Exception:  # noqa: BLE001
        lat = lon = None
    alvo, no_mun, outros = sem_acento(nome), [], []
    for r in _sar_brasil():
        if uf and r['uf'] and r['uf'] != uf:
            continue
        x = dict(r)
        x['km'] = _km(lat, lon, r['lat'], r['lon'])
        x['no_municipio'] = bool(alvo and sem_acento(r['municipio']) == alvo)
        (no_mun if x['no_municipio'] else outros).append(x)
    def ordem(x):
        abastece = x.get('sistema') in ('CANTAREIRA', 'DF', 'RMBH') or 'Reservat' in (x.get('tipo') or '')
        return (0 if abastece else 1, x['km'] is None, x['km'] if x['km'] is not None else 9e9)
    outros.sort(key=ordem)
    lista, vistos = [], set()
    for x in no_mun + outros:
        k = x['nome'] + '|' + x['uf']
        if k in vistos:
            continue
        vistos.add(k)
        lista.append(x)
        if len(lista) == 8:
            break
    return {'municipio': nome, 'reservatorios': lista} if lista else None


# Hortaliças, frutas e básicos vegetais da PAM (sem soja, cana, café — commodities).
PAM_TEMP = ((83375, 'Alface'), (83374, 'Abóbora'), (2690, 'Alho'), (2694, 'Batata-doce'), (2695, 'Batata-inglesa'),
            (2697, 'Cebola'), (83381, 'Cenoura'), (83379, 'Chuchu'), (2702, 'Feijão'), (2708, 'Mandioca'),
            (2709, 'Melancia'), (2710, 'Melão'), (83377, 'Milho verde'), (83376, 'Pimentão'), (83380, 'Repolho'),
            (2715, 'Tomate'))
PAM_PERM = ((2717, 'Abacate'), (2720, 'Banana'), (2724, 'Caqui'), (2730, 'Figo'), (2731, 'Goiaba'), (2733, 'Laranja'),
            (2734, 'Limão'), (2736, 'Mamão'), (2737, 'Manga'), (2738, 'Maracujá'), (2745, 'Tangerina'), (2748, 'Uva'))


def _pam_produtos(tabela, classe, pares, ibge, ano):
    ids = ','.join(str(i) for i, _ in pares)
    try:
        blocos = _ibge(f'{tabela}/periodos/{ano}/variaveis/214?localidades=N6[{ibge}]&classificacao={classe}[{ids}]')
    except Exception:  # noqa: BLE001
        return []
    nomes = {str(i): n for i, n in pares}
    saida = []
    for res in (blocos[0].get('resultados') or []) if blocos else []:
        cat = next(iter((res.get('classificacoes') or [{}])[0].get('categoria') or {}), None)
        serie = ((res.get('series') or [{}])[0].get('serie') or {})
        v = _dec(serie.get(str(ano)))
        if v is None or v <= 0:
            continue
        saida.append({'nome': nomes.get(str(cat), str(cat)), 'toneladas': v})
    saida.sort(key=lambda x: -x['toneladas'])
    return saida


def _censo_n(ibge, **classif):
    q = ''.join(f'&classificacao={k}[{v}]' for k, v in classif.items())
    try:
        r = _ibge(f'6778/periodos/2017/variaveis/183?localidades=N6[{ibge}]{q}')
        serie = ((r[0].get('resultados') or [{}])[0].get('series') or [{}])[0].get('serie') or {}
        return _int_br(serie.get('2017'))
    except Exception:  # noqa: BLE001
        return None


def lavoura(ibge, uf=None):
    """Hortaliças e frutas colhidas no município (PAM, último ano) e quantas fazendas havia no Censo Agro 2017."""
    ibge = str(ibge)
    if len(ibge) < 7:
        return None
    produtos, ano = [], None
    for a in (dt.date.today().year - 1, dt.date.today().year - 2):
        produtos = _pam_produtos(1612, 81, PAM_TEMP, ibge, a) + _pam_produtos(1613, 82, PAM_PERM, ibge, a)
        if produtos:
            ano = a
            break
    produtos.sort(key=lambda x: -x['toneladas'])
    faz = {'ano': 2017, 'total': _censo_n(ibge), 'familiar': _censo_n(ibge, **{'829': 46304}),
           'horticultura': _censo_n(ibge, **{'12517': 111520}), 'frutas': _censo_n(ibge, **{'12517': 111521})}
    if not produtos and not faz['total']:
        return None
    return {'ano': ano, 'produtos': produtos, 'fazendas': faz}


# Faixas etárias do Censo 2022 (tabela 9514) que o observatório agrega.
_IDADE_9514 = (('0_4', '0 a 4 anos', (93070,)), ('5_14', '5 a 14 anos', (93084, 93085)),
               ('15_24', '15 a 24 anos', (93086, 93087)),
               ('25_59', '25 a 59 anos', (93088, 93089, 93090, 93091, 93092, 93093, 93094)),
               ('60_mais', '60 anos ou mais', (93095, 93096, 93097, 93098, 49108, 49109, 60040, 60041, 6653)))
_COR_9605 = ((2776, 'Branca'), (2777, 'Preta'), (2779, 'Parda'), (2778, 'Amarela'), (2780, 'Indígena'))


def perfil_ibge(ibge, uf=None):
    """Retrato sociodemográfico do município: estimativa de moradores, Censo 2022 (idade, sexo, cor ou raça,
    domicílios) e o básico do saneamento. Renda familiar não desce ao município nesta API."""
    ibge = str(ibge)
    if len(ibge) < 7:
        return None
    loc = f'localidades=N6[{ibge}]'
    estimativa = None
    for ano in (dt.date.today().year, dt.date.today().year - 1, dt.date.today().year - 2):
        v = _ibge_vars(f'6579/periodos/{ano}/variaveis/9324?{loc}')
        if v.get('9324') and v['9324'][1]:
            estimativa = {'ano': int(v['9324'][0]), 'pessoas': _inteiro(v['9324'][1])}
            break
    censo = _ibge_vars(f'4709/periodos/2022/variaveis/93|5936|10605?{loc}')
    territorio = _ibge_vars(f'4714/periodos/2022/variaveis/6318|614?{loc}')
    demografia = _ibge_vars(f'9515/periodos/2022/variaveis/10612|10613|8845?{loc}')
    casas = _ibge_vars(f'4712/periodos/2022/variaveis/381|382|5930?{loc}')
    ids_idade = ','.join(str(i) for _, _, ids in _IDADE_9514 for i in ids)
    idades_bruto = {c: v for c, _, v in _ibge_cats(f'9514/periodos/2022/variaveis/93?{loc}&classificacao=2[6794]|287[{ids_idade}]|286[113635]', 287)}
    sexo_bruto = {c: v for c, _, v in _ibge_cats(f'9514/periodos/2022/variaveis/93?{loc}&classificacao=2[4,5]|287[100362]|286[113635]', 2)}
    cor_bruto = {c: v for c, _, v in _ibge_cats(f'9605/periodos/2022/variaveis/93?{loc}&classificacao=86[2776,2777,2778,2779,2780]', 86)}
    agua = {c: v for c, _, v in _ibge_cats(f'6804/periodos/2022/variaveis/381?{loc}&classificacao=301[72053,31471]|1817[72125]', 301)}
    esgoto = {c: v for c, _, v in _ibge_cats(f'6805/periodos/2022/variaveis/381?{loc}&classificacao=11558[46292,46290,72113,92861]', 11558)}
    lixo = {c: v for c, _, v in _ibge_cats(f'6892/periodos/2022/variaveis/381?{loc}&classificacao=67[10972,2520]', 67)}
    alfa = _ibge_vars(f'9543/periodos/2022/variaveis/2513?{loc}&classificacao=2[6794]|86[95251]|287[100362]')
    pop = _inteiro((censo.get('93') or (None, None))[1])
    faixas, grupos = [], {}
    for chave, nome, ids in _IDADE_9514:
        n = sum(idades_bruto.get(str(i)) or 0 for i in ids) or None
        item = {'nome': nome, 'pessoas': _inteiro(n) if n else None, 'percentual': _pct(n, pop)}
        faixas.append(item)
        grupos[chave] = item
    cores = [{'nome': n, 'pessoas': _inteiro(cor_bruto.get(str(i))), 'percentual': _pct(cor_bruto.get(str(i)), pop)}
             for i, n in _COR_9605 if cor_bruto.get(str(i))]
    homens, mulheres = sexo_bruto.get('4'), sexo_bruto.get('5')
    tot_agua, rede = agua.get('72053'), agua.get('31471')
    tot_esgoto, rede_esgoto = esgoto.get('46292'), esgoto.get('46290')
    tot_lixo, coletado = lixo.get('10972'), lixo.get('2520')
    saida = {'censo': {'ano': 2022, 'pessoas': pop, 'variacao_2010': _inteiro((censo.get('5936') or (None, None))[1]),
                       'crescimento_aa': (censo.get('10605') or (None, None))[1]},
             'territorio': {'area_km2': None if not territorio.get('6318') else round(territorio['6318'][1], 1),
                            'densidade': None if not territorio.get('614') else round(territorio['614'][1], 1)},
             'sexo': {'homens': _inteiro(homens), 'mulheres': _inteiro(mulheres),
                      'percentual_homens': _pct(homens, pop), 'percentual_mulheres': _pct(mulheres, pop),
                      'razao': None if not demografia.get('8845') else round(demografia['8845'][1], 1)},
             'idades': {'mediana': _inteiro((demografia.get('10613') or (None, None))[1]),
                        'envelhecimento': None if not demografia.get('10612') else round(demografia['10612'][1], 1),
                        'faixas': faixas, 'grupos': grupos},
             'cor_raca': cores,
             'domicilios': {'total': _inteiro((casas.get('381') or (None, None))[1]),
                            'moradores': _inteiro((casas.get('382') or (None, None))[1]),
                            'media': None if not casas.get('5930') else round(casas['5930'][1], 2)},
             'saneamento': {
                 'agua_rede': {'percentual': _pct(rede, tot_agua), 'domicilios': _inteiro(rede), 'total': _inteiro(tot_agua)},
                 'esgoto_rede': {'percentual': _pct(rede_esgoto, tot_esgoto), 'domicilios': _inteiro(rede_esgoto), 'total': _inteiro(tot_esgoto)},
                 'fossa_rudimentar': _inteiro(esgoto.get('72113')), 'sem_banheiro': _inteiro(esgoto.get('92861')),
                 'lixo_coletado': {'percentual': _pct(coletado, tot_lixo), 'domicilios': _inteiro(coletado), 'total': _inteiro(tot_lixo)}},
             'alfabetizacao': {'ano': 2022, 'idade': '15 anos ou mais',
                               'percentual': None if not alfa.get('2513') else round(alfa['2513'][1], 1)}}
    if estimativa:
        saida['estimativa'] = estimativa
    if not pop and not estimativa:
        return None
    return saida


# ------------------------------------------------------------------ Eco30 · meteorologia e saúde (03/10/2026)
def _ponto(ibge):
    lat, lon = centroide(str(ibge))
    if lat is None:
        raise ValueError('sem centroide')
    return lat, lon


_NORMAIS = {}   # ponto → normais 1991–2020; vale pelo processo inteiro


def _normais(lat, lon):
    """Média 1991–2020 (ERA5 pela Open-Meteo) por dia do ano, janela de 15 dias: {mmdd: [máxima, mínima, chuva mm/dia]}.
    São 30 anos de dados diários (~150 chamadas na cota da Open-Meteo): guardado em disco por ponto, uma vez só."""
    import os, tempfile
    chave = f'{lat:.2f}_{lon:.2f}'
    if chave in _NORMAIS:
        return _NORMAIS[chave]
    try:   # o contêiner do painel é somente leitura e sem /tmp: aí fica só a memória do processo
        pasta = os.environ.get('ECO30_CACHE') or os.path.join(tempfile.gettempdir(), 'eco30-normais')
    except OSError:
        pasta = None
    arq = pasta and os.path.join(pasta, chave + '.json')
    try:
        with open(arq, encoding='utf-8') as f:
            _NORMAIS[chave] = json.load(f)
            return _NORMAIS[chave]
    except (OSError, ValueError, TypeError):
        pass
    d = (_json('https://archive-api.open-meteo.com/v1/archive?' + urllib.parse.urlencode({
        'latitude': lat, 'longitude': lon, 'start_date': '1991-01-01', 'end_date': '2020-12-31', 'timezone': 'America/Sao_Paulo',
        'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum'}), timeout=90) or {}).get('daily') or {}
    por_dia = {}
    for t, mx, mn, ch in zip(d.get('time') or [], d.get('temperature_2m_max') or [], d.get('temperature_2m_min') or [], d.get('precipitation_sum') or []):
        if mx is not None and mn is not None:
            por_dia.setdefault(t[5:], []).append((mx, mn, ch or 0))
    dias = sorted(k for k in por_dia if k != '02-29')
    saida = {}
    for i, k in enumerate(dias):
        janela = [v for j in range(i - 7, i + 8) for v in por_dia[dias[j % len(dias)]]]
        saida[k] = [round(sum(v[c] for v in janela) / len(janela), 1) for c in range(3)]
    if len(saida) < 365:
        raise ValueError('normais incompletas')
    saida['02-29'] = saida['02-28']
    _NORMAIS[chave] = saida
    if pasta:
        try:
            os.makedirs(pasta, exist_ok=True)
            with open(arq, 'w', encoding='utf-8') as f:
                json.dump(saida, f)
        except OSError:
            pass
    return saida


ONDA_ANOMALIA, ONDA_DIAS = 5, 5   # INMET: máxima 5 °C acima da média por 5 dias ou mais


def calor(ibge, uf=None):
    """Máximas dos últimos 30 dias e dos próximos 7 ao lado do normal 1991–2020 do mesmo ponto, onda de calor pelo critério
    do INMET, sensação e UV. Normal = reanálise ERA5; dias = modelos da Open-Meteo (estimativa no centro do município)."""
    lat, lon = _ponto(ibge)
    normal = _normais(lat, lon)
    d = (_json('https://api.open-meteo.com/v1/forecast?' + urllib.parse.urlencode({
        'latitude': lat, 'longitude': lon, 'timezone': 'America/Sao_Paulo', 'past_days': 30, 'forecast_days': 8,
        'daily': 'temperature_2m_max,temperature_2m_min,apparent_temperature_max,uv_index_max,precipitation_sum'}), timeout=30) or {}).get('daily') or {}
    hoje = dt.datetime.now(BRT).date().isoformat()
    dias = []
    for t, mx, mn, sx, uv, ch in zip(*(d.get(k) or [] for k in ('time', 'temperature_2m_max', 'temperature_2m_min', 'apparent_temperature_max', 'uv_index_max', 'precipitation_sum'))):
        nm = normal.get(t[5:])
        if mx is None or not nm:
            continue
        dias.append({'dia': t, 'previsao': t > hoje, 'max': _num(mx), 'min': _num(mn), 'sensacao_max': _num(sx), 'uv_max': _num(uv),
                     'chuva_mm': _num(ch), 'normal_max': nm[0], 'normal_min': nm[1], 'anomalia': _num(mx - nm[0])})
    if not dias:
        return None
    ondas, seq = [], []
    for x in dias + [None]:
        if x and x['anomalia'] is not None and x['anomalia'] >= ONDA_ANOMALIA:
            seq.append(x)
            continue
        if len(seq) >= ONDA_DIAS:
            ondas.append({'de': seq[0]['dia'], 'ate': seq[-1]['dia'], 'dias': len(seq), 'max': max(s['max'] for s in seq),
                          'previsao': seq[-1]['previsao']})
        seq = []
    passado = [x for x in dias if not x['previsao']][-30:]
    mes = {'dias': len(passado), 'de': passado[0]['dia'], 'ate': passado[-1]['dia'],
           'chuva_mm': _num(sum(x['chuva_mm'] or 0 for x in passado)), 'chuva_normal_mm': _num(sum(normal[x['dia'][5:]][2] for x in passado)),
           'max_media': _num(sum(x['max'] for x in passado) / len(passado)), 'max_normal': _num(sum(x['normal_max'] for x in passado) / len(passado)),
           'dias_acima': sum(1 for x in passado if x['anomalia'] >= ONDA_ANOMALIA)} if passado else None
    return {'lat': lat, 'lon': lon, 'normal': '1991–2020', 'dias': dias[-15:], 'ondas': ondas, 'mes': mes}


OMS_AR = {'pm2_5': ('PM2.5', 15, '24 h'), 'pm10': ('PM10', 45, '24 h'), 'nitrogen_dioxide': ('NO₂', 25, '24 h'), 'ozone': ('Ozônio', 100, '8 h')}


def ar(ibge, uf=None):
    """Qualidade do ar estimada (Copernicus CAMS pela Open-Meteo) no centro do município: médias das últimas 24 h
    (ozônio: pior média de 8 h) e o pico das próximas 24 h, ao lado das diretrizes da OMS (2021). Modelo, não estação."""
    lat, lon = _ponto(ibge)
    h = (_json('https://air-quality-api.open-meteo.com/v1/air-quality?' + urllib.parse.urlencode({
        'latitude': lat, 'longitude': lon, 'timezone': 'America/Sao_Paulo', 'past_days': 1, 'forecast_days': 2,
        'hourly': ','.join(OMS_AR) + ',uv_index'}), timeout=30) or {}).get('hourly') or {}
    horas = h.get('time') or []
    agora = dt.datetime.now(BRT).strftime('%Y-%m-%dT%H:00')
    i = next((k for k, t in enumerate(horas) if t >= agora), len(horas)) if horas else 0
    if i < 24:
        return None
    poluentes = []
    for chave, (nome, limite, janela) in OMS_AR.items():
        v = h.get(chave) or []
        antes, depois = [x for x in v[i - 24:i] if x is not None], [x for x in v[i:i + 24] if x is not None]
        if not antes:
            continue
        if janela == '8 h':
            medias = [sum(antes[k:k + 8]) / 8 for k in range(len(antes) - 7)] or [sum(antes) / len(antes)]
            valor = max(medias)
        else:
            valor = sum(antes) / len(antes)
        poluentes.append({'id': chave, 'nome': nome, 'valor': _num(valor), 'oms': limite, 'janela': janela,
                          'acima': valor > limite, 'pico_24h': _num(max(depois)) if depois else None})
    uv = [x for x in (h.get('uv_index') or [])[i:i + 24] if x is not None]
    return {'lat': lat, 'lon': lon, 'hora': horas[i] if i < len(horas) else None, 'poluentes': poluentes,
            'uv_max_24h': _num(max(uv)) if uv else None} if poluentes else None


INPE_FOCOS = 'https://dataserver-coids.inpe.br/queimadas/queimadas/focos/csv/diario/Brasil/focos_diario_br_{}.csv'
INPE_REFERENCIA = 'AQUA_M-T'   # satélite de referência do INPE: o único comparável entre dias e anos
QUEIMADAS_DIAS = 7
_FOCOS = {}   # dia → {ibge7: [referência, detecções, maior nº de dias sem chuva, maior risco de fogo], 'uf:NN': referência}


def _focos_dia(dia):
    if dia in _FOCOS:
        return _FOCOS[dia]
    import csv, io
    texto = _get(INPE_FOCOS.format(dia.strftime('%Y%m%d')), timeout=90).decode('utf-8', 'ignore')
    agg = {}
    for r in csv.DictReader(io.StringIO(texto)):
        ib, ref = (r.get('municipio_id') or '').strip(), (r.get('satelite') or '').strip() == INPE_REFERENCIA
        if not re.fullmatch(r'\d{7}', ib):
            continue
        a = agg.setdefault(ib, [0, 0, None, None])
        a[0] += ref
        a[1] += 1
        sc, rf = _dec(r.get('numero_dias_sem_chuva')), _dec(r.get('risco_fogo'))
        if sc is not None and sc >= 0:
            a[2] = max(a[2] or 0, int(sc))
        if rf is not None and 0 <= rf <= 1:
            a[3] = max(a[3] or 0, rf)
        if ref:
            agg['uf:' + ib[:2]] = agg.get('uf:' + ib[:2], 0) + 1
    for k in [k for k in _FOCOS if k < dia - dt.timedelta(days=QUEIMADAS_DIAS + 2)]:
        del _FOCOS[k]
    _FOCOS[dia] = agg
    return agg


def queimadas(ibge, uf=None):
    """Focos de queimada detectados pelo INPE nos últimos 7 dias fechados: no município (satélite de referência e todas
    as detecções, que repetem o mesmo fogo) e no estado (referência), com dias sem chuva e risco de fogo dos focos."""
    ibge, ontem = str(ibge), dt.datetime.now(BRT).date() - dt.timedelta(days=1)
    por_dia, lidos = [], 0
    for k in range(QUEIMADAS_DIAS):
        dia = ontem - dt.timedelta(days=k)
        try:
            agg = _focos_dia(dia)
        except Exception:  # noqa: BLE001
            continue
        lidos += 1
        m = agg.get(ibge) or [0, 0, None, None]
        por_dia.append({'dia': dia.isoformat(), 'referencia': m[0], 'deteccoes': m[1], 'sem_chuva': m[2], 'risco': m[3],
                        'estado': agg.get('uf:' + ibge[:2], 0)})
    if not lidos:
        return None
    por_dia.reverse()
    def mx(c):
        v = [x[c] for x in por_dia if x[c] is not None]
        return max(v) if v else None
    return {'de': por_dia[0]['dia'], 'ate': por_dia[-1]['dia'], 'dias': lidos, 'satelite_referencia': INPE_REFERENCIA,
            'municipio': {'referencia': sum(x['referencia'] for x in por_dia), 'deteccoes': sum(x['deteccoes'] for x in por_dia),
                          'sem_chuva': mx('sem_chuva'), 'risco': mx('risco')},
            'estado': {'referencia': sum(x['estado'] for x in por_dia)}, 'por_dia': por_dia}


# ------------------------------------------------------------------ Eco30 · metabólicos (06/10/2026)
# PNS (IBGE): adultos com diagnóstico médico referido. A amostra só comporta Brasil, UF e capital (nível N6 só nas capitais).
PNS_TABELAS = (('hipertensao', 'Hipertensão arterial', 4418, 4399), ('diabetes', 'Diabetes', 4487, 4465),
               ('colesterol', 'Colesterol alto', 4547, 4549))
PNS_OBESIDADE = (8168, 11683, 11675)   # tabela, % obesidade, % excesso de peso: medidos pelo IBGE, só Brasil
CAPITAIS = {'11': '1100205', '12': '1200401', '13': '1302603', '14': '1400100', '15': '1501402', '16': '1600303', '17': '1721000',
            '21': '2111300', '22': '2211001', '23': '2304400', '24': '2408102', '25': '2507507', '26': '2611606', '27': '2704302',
            '28': '2800308', '29': '2927408', '31': '3106200', '32': '3205309', '33': '3304557', '35': '3550308', '41': '4106902',
            '42': '4205407', '43': '4314902', '50': '5002704', '51': '5103403', '52': '5208707', '53': '5300108'}
_PNS = {}   # tabela → {código da localidade: {ano: %}}, 'nomes' → {código: nome}; muda a cada edição da pesquisa (2013, 2019)


def _pns_tabela(tabela, variavel):
    if tabela in _PNS:
        return _PNS[tabela]
    blocos = _ibge(f'{tabela}/periodos/-6/variaveis/{variavel}?localidades=N1[all]|N3[all]|N6[{",".join(CAPITAIS.values())}]'
                   '&classificacao=2[6794]|1[6795]') or []
    saida, nomes = {}, _PNS.setdefault('nomes', {})
    for res in (blocos[0].get('resultados') or []) if blocos else []:
        for s in res.get('series') or []:
            loc = s.get('localidade') or {}
            pontos = {int(a): _dec(v) for a, v in (s.get('serie') or {}).items() if _dec(v) is not None}
            if pontos:
                saida[str(loc.get('id'))] = pontos
                nomes[str(loc.get('id'))] = loc.get('nome')
    if saida:
        _PNS[tabela] = saida
    return saida


def pns(ibge, uf=None):
    """Hipertensão, diabetes e colesterol alto referidos pelos adultos na PNS (2013 e 2019) no Brasil, no estado e na
    capital do estado, mais obesidade e excesso de peso medidos (só Brasil). Retrato do estado, não do município."""
    ibge, cod = str(ibge), str(ibge)[:2]
    capital = CAPITAIS.get(cod)
    indicadores = []
    for chave, nome, tabela, variavel in PNS_TABELAS:
        t = _pns_tabela(tabela, variavel)
        br, est, cap = t.get('1') or {}, t.get(cod) or {}, t.get(capital) or {}
        anos = sorted(set(br) | set(est))
        if anos:
            indicadores.append({'id': chave, 'nome': nome, 'pontos': [{'ano': a, 'brasil': br.get(a), 'estado': est.get(a), 'capital': cap.get(a)} for a in anos]})
    if not indicadores:
        return None
    tabela, v_ob, v_ex = PNS_OBESIDADE
    obesidade = []
    try:
        blocos = _ibge(f'{tabela}/periodos/-6/variaveis/{v_ob}|{v_ex}?localidades=N1[all]&classificacao=2[6794]|58[95253]') or []
        por_var = {str(b.get('id')): ((b.get('resultados') or [{}])[0].get('series') or [{}])[0].get('serie') or {} for b in blocos}
        for a in sorted(set(por_var.get(str(v_ob), {})) | set(por_var.get(str(v_ex), {}))):
            obesidade.append({'ano': int(a), 'obesidade': _dec(por_var.get(str(v_ob), {}).get(a)), 'excesso': _dec(por_var.get(str(v_ex), {}).get(a))})
    except Exception:  # noqa: BLE001
        pass
    nomes = _PNS.get('nomes', {})
    return {'pesquisa': 'PNS', 'unidade': '% dos adultos (18 anos ou mais)', 'estado': nomes.get(cod) or uf,
            'capital': nomes.get(capital), 'e_capital': ibge == capital, 'indicadores': indicadores, 'obesidade_brasil': obesidade}


# fonte → (função, validade em horas). Quem grava (cron ou painel) decide quando buscar de novo.
INDICADORES = {'sisagua': (sisagua, 24), 'zika': (zika, 24), 'febre_amarela': (febre_amarela, 24),
               'ubs': (ubs, 24 * 7), 'previne': (previne, 24 * 7), 'sisvan': (sisvan, 24 * 30),
               'ibge_alimentacao': (ibge_alimentacao, 24 * 30), 'reservatorios': (reservatorios, 24),
               'lavoura': (lavoura, 24 * 30), 'perfil_ibge': (perfil_ibge, 24 * 30),
               'comparacao': (comparacao, 24 * 7), 'calor': (calor, 6), 'ar': (ar, 3), 'queimadas': (queimadas, 12), 'pns': (pns, 24 * 30)}


def indicador(fonte, ibge, uf=None):
    return INDICADORES[fonte][0](str(ibge), (uf or '').upper() or None)
