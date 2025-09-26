{{ config(
    materialized = 'table',
    unique_key = 'web_page_sk'
) }}

with base as (
    select
        wp_web_page_sk     as web_page_sk,
        wp_web_page_id     as web_page_id,
        wp_rec_start_date  as rec_start_date,
        wp_rec_end_date    as rec_end_date,
        wp_creation_date_sk as creation_date_sk,
        wp_access_date_sk  as access_date_sk,
        wp_autogen_flag    as autogen_flag,
        wp_customer_sk     as customer_sk,
        wp_url             as url,
        wp_type            as type,
        wp_char_count      as char_count,
        wp_link_count      as link_count,
        wp_image_count     as image_count,
        wp_max_ad_count    as max_ad_count
    from {{ source('tpcds','web_page') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['web_page_sk']) }} as dim_web_page_sk,
    *
from base

