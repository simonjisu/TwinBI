{{ config(
    materialized = 'table',
    unique_key = 'ship_mode_sk'
) }}

with base as (
    select
        sm_ship_mode_sk,
        sm_ship_mode_id,
        sm_type,
        sm_code,
        sm_carrier,
        sm_contract
    from {{ source('tpcds','ship_mode') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['ship_mode_sk']) }} as dim_ship_mode_sk,
    *
from base

