{{ config(
    materialized = 'table',
    unique_key = 'address_sk'
) }}

with base as (
    select
        ca_address_sk    as address_sk,
        ca_address_id    as address_id,
        ca_street_number as street_number,
        ca_street_name   as street_name,
        ca_street_type   as street_type,
        ca_suite_number  as suite_number,
        ca_city          as city,
        ca_county        as county,
        ca_state         as state,
        ca_zip           as zip,
        ca_country       as country,
        ca_gmt_offset    as gmt_offset,
        ca_location_type as location_type
    from {{ source('tpcds','customer_address') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['address_sk']) }} as dim_address_sk,
    *
from base

