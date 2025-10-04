{{ config(
    materialized = 'table',
    unique_key = 'household_demo_sk'
) }}

with base as (
    select
        hd_demo_sk,
        hd_income_band_sk,
        hd_buy_potential,
        hd_dep_count,
        hd_vehicle_count
    from {{ source('tpcds','household_demographics') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['household_demo_sk']) }} as dim_household_sk,
    *
from base

