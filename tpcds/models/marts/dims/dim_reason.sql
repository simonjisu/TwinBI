{{ config(
    materialized = 'table',
    unique_key = 'reason_sk'
) }}

with base as (
    select
        r_reason_sk  as reason_sk,
        r_reason_id  as reason_id,
        r_reason_desc as reason_desc
    from {{ source('tpcds','reason') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['reason_sk']) }} as dim_reason_sk,
    *
from base

