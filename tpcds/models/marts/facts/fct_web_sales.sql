{{ config(materialized='table') }}

with base as (
  select *
  from {{ ref('stg_web_sales') }}
)

select
  -- native keys
  b.ws_sold_date_sk,
  b.ws_sold_time_sk,
  b.ws_ship_date_sk,
  b.ws_item_sk,
  b.ws_bill_customer_sk,
  b.ws_bill_cdemo_sk,
  b.ws_bill_hdemo_sk,
  b.ws_bill_addr_sk,
  b.ws_ship_customer_sk,
  b.ws_ship_cdemo_sk,
  b.ws_ship_hdemo_sk,
  b.ws_ship_addr_sk,
  b.ws_web_page_sk,
  b.ws_web_site_sk,
  b.ws_ship_mode_sk,
  b.ws_warehouse_sk,
  b.ws_promo_sk,
  -- measures
  b.ws_order_number,
  b.ws_quantity,
  b.ws_wholesale_cost,
  b.ws_list_price,
  b.ws_sales_price,
  b.ws_ext_discount_amt,
  b.ws_ext_sales_price,
  b.ws_ext_wholesale_cost,
  b.ws_ext_list_price,
  b.ws_ext_tax,
  b.ws_coupon_amt,
  b.ws_ext_ship_cost,
  b.ws_net_paid,
  b.ws_net_paid_inc_tax,
  b.ws_net_paid_inc_ship,
  b.ws_net_paid_inc_ship_tax,
  b.ws_net_profit,
  -- surrogate keys for joins
  dds.dim_date_sk            as sold_date_dk,
  dts.dim_time_sk            as sold_time_dk,
  dsh.dim_date_sk            as ship_date_dk,
  di.dim_item_sk             as item_dk,
  dbc.dim_customer_sk        as bill_customer_dk,
  dbcd.dim_customer_demo_sk  as bill_cdemo_dk,
  dbh.dim_household_sk       as bill_hdemo_dk,
  dba.dim_address_sk         as bill_addr_dk,
  dsc.dim_customer_sk        as ship_customer_dk,
  dscd.dim_customer_demo_sk  as ship_cdemo_dk,
  dshh.dim_household_sk      as ship_hdemo_dk,
  dsa.dim_address_sk         as ship_addr_dk,
  dwp.dim_web_page_sk        as web_page_dk,
  dws.dim_web_site_sk        as web_site_dk,
  dsm.dim_ship_mode_sk       as ship_mode_dk,
  dw.dim_warehouse_sk        as warehouse_dk,
  dp.dim_promotion_sk        as promo_dk

from base b
left join {{ ref('dim_date') }}          dds  on b.ws_sold_date_sk     = dds.date_sk
left join {{ ref('dim_time') }}          dts  on b.ws_sold_time_sk     = dts.time_sk
left join {{ ref('dim_date') }}          dsh  on b.ws_ship_date_sk     = dsh.date_sk
left join {{ ref('dim_item') }}          di   on b.ws_item_sk          = di.item_sk
left join {{ ref('dim_customer') }}      dbc  on b.ws_bill_customer_sk = dbc.customer_sk
left join {{ ref('dim_customer_demo') }} dbcd on b.ws_bill_cdemo_sk    = dbcd.customer_demo_sk
left join {{ ref('dim_household') }}     dbh  on b.ws_bill_hdemo_sk    = dbh.household_demo_sk
left join {{ ref('dim_address') }}       dba  on b.ws_bill_addr_sk     = dba.address_sk
left join {{ ref('dim_customer') }}      dsc  on b.ws_ship_customer_sk = dsc.customer_sk
left join {{ ref('dim_customer_demo') }} dscd on b.ws_ship_cdemo_sk    = dscd.customer_demo_sk
left join {{ ref('dim_household') }}     dshh on b.ws_ship_hdemo_sk    = dshh.household_demo_sk
left join {{ ref('dim_address') }}       dsa  on b.ws_ship_addr_sk     = dsa.address_sk
left join {{ ref('dim_web_page') }}      dwp  on b.ws_web_page_sk      = dwp.web_page_sk
left join {{ ref('dim_web_site') }}      dws  on b.ws_web_site_sk      = dws.web_site_sk
left join {{ ref('dim_ship_mode') }}     dsm  on b.ws_ship_mode_sk     = dsm.ship_mode_sk
left join {{ ref('dim_warehouse') }}     dw   on b.ws_warehouse_sk     = dw.warehouse_sk
left join {{ ref('dim_promotion') }}     dp   on b.ws_promo_sk         = dp.promo_sk

