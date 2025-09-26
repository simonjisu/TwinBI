{{ config(
    materialized = 'table',
    unique_key = 'customer_sk'
) }}

with base as (
    select
        c_customer_sk            as customer_sk,
        c_customer_id            as customer_id,
        c_current_cdemo_sk       as current_cdemo_sk,
        c_current_hdemo_sk       as current_hdemo_sk,
        c_current_addr_sk        as current_addr_sk,
        c_first_shipto_date_sk   as first_shipto_date_sk,
        c_first_sales_date_sk    as first_sales_date_sk,
        c_salutation             as salutation,
        c_first_name             as first_name,
        c_last_name              as last_name,
        c_preferred_cust_flag    as preferred_cust_flag,
        c_birth_day              as birth_day,
        c_birth_month            as birth_month,
        c_birth_year             as birth_year,
        c_birth_country          as birth_country,
        c_login                  as login,
        c_email_address          as email_address,
        c_last_review_date_sk    as last_review_date_sk
    from {{ source('tpcds','customer') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['customer_sk']) }} as dim_customer_sk,
    *
from base

