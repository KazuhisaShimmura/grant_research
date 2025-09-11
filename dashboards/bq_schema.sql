-- BigQuery DDL
CREATE SCHEMA IF NOT EXISTS `grants_hc`;

CREATE TABLE IF NOT EXISTS `grants_hc.programs` (
  program_id STRING, source_url STRING, publisher STRING, title STRING,
  fiscal_year STRING, domain STRING, eligibility_json STRING, geography STRING,
  subsidy_rate FLOAT64, subsidy_cap_jpy INT64, budget_total_jpy INT64,
  cost_items_allowed STRING, deadline_type STRING, deadline_at TIMESTAMP,
  application_method STRING, requires_gbizid BOOL, docs_urls_json STRING,
  status STRING, published_at TIMESTAMP, last_seen_at TIMESTAMP, content_hash STRING,
  dedupe_key STRING
)
PARTITION BY DATE(last_seen_at);
