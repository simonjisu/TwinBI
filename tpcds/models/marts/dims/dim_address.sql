{{ config(
    materialized = 'table',
    unique_key = 'address_sk'
) }}

with base as (
    select
        ca_address_sk,
        ca_address_id,
        ca_street_number,
        ca_street_name,
        ca_street_type,
        ca_suite_number,
        ca_city,
        ca_county,
        ca_state,
        ca_zip,
        ca_country,
        ca_gmt_offset,
        ca_location_type
    from {{ source('tpcds','customer_address') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['address_sk']) }} as dim_address_sk,
    *
from base

