{{ config(
    materialized = 'table',
    unique_key = 'time_sk'
) }}

with base as (
    select
        t_time_sk   as time_sk,
        t_time_id   as time_id,
        t_time      as time_actual,
        t_hour      as hour,
        t_minute    as minute,
        t_second    as second,
        t_am_pm     as am_pm,
        t_shift     as shift,
        t_sub_shift as sub_shift,
        t_meal_time as meal_time
    from {{ source('tpcds','time_dim') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['time_sk']) }} as dim_time_sk,
    *
from base

