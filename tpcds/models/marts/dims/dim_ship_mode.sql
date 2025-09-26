{{ config(
    materialized = 'table',
    unique_key = 'ship_mode_sk'
) }}

with base as (
    select
        sm_ship_mode_sk as ship_mode_sk,
        sm_ship_mode_id as ship_mode_id,
        sm_type         as type,
        sm_code         as code,
        sm_carrier      as carrier,
        sm_contract     as contract
    from {{ source('tpcds','ship_mode') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['ship_mode_sk']) }} as dim_ship_mode_sk,
    *
from base

