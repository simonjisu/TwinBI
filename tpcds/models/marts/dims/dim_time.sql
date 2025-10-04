{{ config(
    materialized = 'table',
    unique_key = 'time_sk'
) }}

with base as (
    select
        t_time_sk,
        t_time_id,
        t_time,
        t_hour,
        t_minute,
        t_second,
        t_am_pm,
        t_shift,
        t_sub_shift,
        t_meal_time
    from {{ source('tpcds','time_dim') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['time_sk']) }} as dim_time_sk,
    *
from base

