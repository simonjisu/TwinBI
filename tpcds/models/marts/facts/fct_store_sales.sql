{{ config(materialized='table') }}

with base as (
  -- 원본 staging: 컬럼명 그대로 유지
  select *
  from {{ ref('stg_store_sales') }}
)

select
  -- native keys
  b.ss_sold_date_sk,
  b.ss_sold_time_sk,
  b.ss_item_sk,
  b.ss_customer_sk,
  b.ss_cdemo_sk,
  b.ss_hdemo_sk,
  b.ss_addr_sk,
  b.ss_store_sk,
  b.ss_promo_sk,
  -- measures
  b.ss_ticket_number,
  b.ss_quantity,
  b.ss_wholesale_cost,
  b.ss_list_price,
  b.ss_sales_price,
  b.ss_ext_discount_amt,
  b.ss_ext_sales_price,
  b.ss_ext_wholesale_cost,
  b.ss_ext_list_price,
  b.ss_ext_tax,
  b.ss_coupon_amt,
  b.ss_net_paid,
  b.ss_net_paid_inc_tax,
  b.ss_net_profit
  -- surrogate keys: convinience for joins
  dd.dim_date_sk               as sold_date_dk,
  dt.dim_time_sk               as sold_time_dk,
  di.dim_item_sk               as item_dk,
  dc.dim_customer_sk           as customer_dk,
  dcd.dim_customer_demo_sk     as cdemo_dk,
  dh.dim_household_sk          as hdemo_dk,
  da.dim_address_sk            as addr_dk,
  ds.dim_store_sk              as store_dk,
  dp.dim_promotion_sk          as promo_dk

from base b
left join {{ ref('dim_date') }}              dd  on b.ss_sold_date_sk = dd.d_date_sk
left join {{ ref('dim_time') }}              dt  on b.ss_sold_time_sk = dt.t_time_sk
left join {{ ref('dim_item') }}              di  on b.ss_item_sk      = di.i_item_sk
left join {{ ref('dim_customer') }}          dc  on b.ss_customer_sk  = dc.c_customer_sk
left join {{ ref('dim_customer_demo') }}     dcd on b.ss_cdemo_sk     = dcd.cd_demo_sk
left join {{ ref('dim_household') }}         dh  on b.ss_hdemo_sk     = dh.hd_demo_sk
left join {{ ref('dim_address') }}           da  on b.ss_addr_sk      = da.ca_address_sk
left join {{ ref('dim_store') }}             ds  on b.ss_store_sk     = ds.s_store_sk
left join {{ ref('dim_promotion') }}         dp  on b.ss_promo_sk     = dp.p_promo_sk
