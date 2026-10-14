-- Tabelas usadas pelos coletores do observatório (cópia do esquema em produção, 14/10/2026). Postgres 13+.
CREATE SCHEMA IF NOT EXISTS painel;
CREATE TABLE IF NOT EXISTS painel.regiao_indicador (
  ibge          text NOT NULL,
  fonte         text NOT NULL,                       -- sisagua | zika | febre_amarela | ubs | previne | sisvan
  dados         jsonb,                               -- NULL = a fonte respondeu sem dado para o município
  atualizado_em timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (ibge, fonte)
);
-- 02/10/2026: snapshot por período (ano do SISVAN, mês da água, quadrimestre do Previne) para tendência.
-- Não é cópia horária: a chave é o período que o dado descreve. Reconsultar o mesmo ano só atualiza a linha.
CREATE TABLE IF NOT EXISTS painel.regiao_indicador_snapshot (
  ibge          text NOT NULL,
  fonte         text NOT NULL,
  periodo       text NOT NULL,                       -- 2024 | 2024-03 | 20242 (quadrimestre)
  dados         jsonb NOT NULL,
  coletado_em   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (ibge, fonte, periodo)
);
CREATE INDEX IF NOT EXISTS regiao_indicador_snapshot_fonte ON painel.regiao_indicador_snapshot (fonte, periodo);
-- 02/10/2026: doses do PNI contadas por município de residência e mês (deploy/painel/vacinacao.py) e os meses já lidos.
CREATE TABLE IF NOT EXISTS painel.regiao_vacina (
  ibge   text NOT NULL,                                -- 6 dígitos, como no PNI
  ano    int NOT NULL,
  mes    int NOT NULL,
  vacina text NOT NULL,                                -- bcg | penta | polio | pneumo | rota | menc | scr
  doses  int NOT NULL,
  PRIMARY KEY (ibge, ano, mes, vacina)
);
CREATE TABLE IF NOT EXISTS painel.regiao_vacina_arquivo (
  ano        int NOT NULL,
  mes        int NOT NULL,
  modificado text,                                     -- Last-Modified do zip: mudou, lê de novo
  linhas     bigint,
  lido_em    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (ano, mes)
);
-- 02/10/2026: consumo alimentar do SISVAN por região de saúde (deploy/painel/consumo.py), uma linha por estado × marcador × ano
CREATE TABLE IF NOT EXISTS painel.sisvan_consumo (
  uf        text NOT NULL,                             -- código IBGE do estado (2 dígitos)
  indicador text NOT NULL,                             -- ame | ultra_bebe | ultra_crianca | feijao | fruta | verdura | ultra_adulto
  ano       int NOT NULL,
  dados     jsonb NOT NULL,                            -- {regioes: {nome: {sim, total, percentual}}, estado, brasil}
  lido_em   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (uf, indicador, ano)
);
CREATE TABLE IF NOT EXISTS painel.sisvan_consumo_falha (     -- consulta que caiu: volta para o fim da fila
  uf        text NOT NULL,
  indicador text NOT NULL,
  motivo    text,
  em        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (uf, indicador)
);
