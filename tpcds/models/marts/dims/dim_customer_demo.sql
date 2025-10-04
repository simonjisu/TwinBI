{{ config(
    materialized = 'table',
    unique_key = 'customer_demo_sk'
) }}

with base as (
    select
        cd_demo_sk,
        cd_gender,
        cd_marital_status,
        cd_education_status,
        cd_purchase_estimate,
        cd_credit_rating,
        cd_dep_count,
        cd_dep_employed_count,
        cd_dep_college_count
    from {{ source('tpcds','customer_demographics') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['customer_demo_sk']) }} as dim_customer_demo_sk,
    *
from base

