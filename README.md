# Coletores do observatório de saúde pública

Código aberto (MIT) dos coletores que alimentam o observatório de dados públicos de saúde publicado em
<https://www.anaceregatti.com.br/blog/biblioteca/dados-publicos-saude> (powered by Co-labore). Publicado para que
jornalistas, pesquisadores e quem mais quiser possam **conferir de onde vem cada número e como ele é calculado**.

Só dado público e agregado, buscado nas fontes oficiais. Nenhum dado pessoal entra ou sai daqui.

## O que tem aqui

| Arquivo | O que faz |
|---|---|
| `coletores/regiao_fontes.py` | Busca e normaliza, sem gravar: InfoDengue, API de Dados Abertos do Ministério da Saúde (zika, febre amarela, SISAGUA, CNES, Previne), SISVAN (relatórios públicos), IBGE (agregados e localidades), ANA (reservatórios), INMET, Open-Meteo e publicações do MS, da OMS e da OPAS |
| `coletores/ripsa.py` | Nascimentos (SINASC), mortalidade (SIM) e internações sensíveis à atenção primária (SIH) por município, a partir dos dados abertos do SUS (indicadores RIPSA) |
| `coletores/vacinacao.py` | Cobertura vacinal por município a partir dos microdados do PNI |
| `coletores/consumo.py` | Marcadores de consumo alimentar do SISVAN por região de saúde |
| `esquema.sql` | As tabelas em que os coletores gravam (Postgres 13+) |

## Como rodar

```bash
psql -f esquema.sql                      # cria o esquema painel e as tabelas
export PGHOST=... PGUSER=... PGDATABASE=...   # variáveis padrão do psql
# opcional: outro comando para chegar ao banco (ex.: dentro de um contêiner)
export OBSERVATORIO_PSQL='psql'
# o uso de cada coletor está no cabeçalho do arquivo
python3 coletores/ripsa.py
```

Python 3.9+, só a biblioteca padrão. Os coletores pedem as fontes com calma: respeite o mesmo ritmo.

## Licenças das fontes (conferidas nos documentos oficiais em 14/10/2026)

- **Base geral:** Lei 14.129/2021, art. 29 (dados de livre utilização), e Decreto 8.777/2016, art. 4º (redação do
  Decreto 9.903/2019): os dados abertos do Executivo federal podem ser usados livremente, com crédito à fonte.
- **Portal de Dados Abertos do SUS:** no metadado de cada conjunto, SIM, SINASC, CNES, SISAGUA, arboviroses, febre
  amarela e SISVAN estão em Creative Commons Atribuição. PNI (doses aplicadas) e os conjuntos RIPSA não trazem licença
  no metadado e seguem a regra geral acima. O cabeçalho do portal cita CC BY-ND 3.0, em conflito com o metadado e com
  a lei. O próprio Plano de Dados Abertos do MS 2026-2028 lista a definição das licenças como pendente. Qual dos dois
  prevalece: **não confirmado**.
- **IBGE:** livre reuso com citação da fonte e nota de elaboração própria.
- **Open-Meteo:** CC BY 4.0. A API gratuita é só para uso não comercial.
- **OpenStreetMap/Nominatim:** ODbL, com atribuição e política de uso do Nominatim.
- **InfoDengue e ViaCEP:** **não confirmado**.

Cite sempre a fonte original de cada número, além deste código.

## Contato

Co-labore · <https://www.co-labore.com>
