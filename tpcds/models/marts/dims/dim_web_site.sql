{{ config(
    materialized = 'table',
    unique_key = 'web_site_sk'
) }}

with base as (
    select
        web_site_sk       as web_site_sk,
        web_site_id       as web_site_id,
        web_rec_start_date as rec_start_date,
        web_rec_end_date  as rec_end_date,
        web_name          as name,
        web_open_date_sk  as open_date_sk,
        web_close_date_sk as close_date_sk,
        web_class         as class,
        web_manager       as manager,
        web_mkt_id        as mkt_id,
        web_mkt_class     as mkt_class,
        web_mkt_desc      as mkt_desc,
        web_market_manager as market_manager,
        web_company_id    as company_id,
        web_company_name  as company_name,
        web_street_number as street_number,
        web_street_name   as street_name,
        web_street_type   as street_type,
        web_suite_number  as suite_number,
        web_city          as city,
        web_county        as county,
        web_state         as state,
        web_zip           as zip,
        web_country       as country,
        web_gmt_offset    as gmt_offset,
        web_tax_percentage as tax_percentage
    from {{ source('tpcds','web_site') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['web_site_sk']) }} as dim_web_site_sk,
    *
from base

