{{ config(
    materialized = 'table',
    unique_key = 'catalog_page_sk'
) }}

with base as (
    select
        cp_catalog_page_sk,
        cp_catalog_page_id,
        cp_start_date_sk,
        cp_end_date_sk,
        cp_department,
        cp_catalog_number,
        cp_catalog_page_number,
        cp_description,
        cp_type
    from {{ source('tpcds','catalog_page') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['catalog_page_sk']) }} as dim_catalog_page_sk,
    *
from base

