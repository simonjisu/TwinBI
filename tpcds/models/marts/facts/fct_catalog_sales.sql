{{ config(materialized='table') }}

with base as (
  select *
  from {{ ref('stg_catalog_sales') }}
)

select
  -- native keys
  b.cs_sold_date_sk,
  b.cs_sold_time_sk,
  b.cs_ship_date_sk,
  b.cs_bill_customer_sk,
  b.cs_bill_cdemo_sk,
  b.cs_bill_hdemo_sk,
  b.cs_bill_addr_sk,
  b.cs_ship_customer_sk,
  b.cs_ship_cdemo_sk,
  b.cs_ship_hdemo_sk,
  b.cs_ship_addr_sk,
  b.cs_call_center_sk,
  b.cs_catalog_page_sk,
  b.cs_ship_mode_sk,
  b.cs_warehouse_sk,
  b.cs_item_sk,
  b.cs_promo_sk,
  -- measures
  b.cs_order_number,
  b.cs_quantity,
  b.cs_wholesale_cost,
  b.cs_list_price,
  b.cs_sales_price,
  b.cs_ext_discount_amt,
  b.cs_ext_sales_price,
  b.cs_ext_wholesale_cost,
  b.cs_ext_list_price,
  b.cs_ext_tax,
  b.cs_coupon_amt,
  b.cs_ext_ship_cost,
  b.cs_net_paid,
  b.cs_net_paid_inc_tax,
  b.cs_net_paid_inc_ship,
  b.cs_net_paid_inc_ship_tax,
  b.cs_net_profit,
  -- surrogate keys for joins
  dds.dim_date_sk            as sold_date_dk,
  dts.dim_time_sk            as sold_time_dk,
  dsh.dim_date_sk            as ship_date_dk,
  dbc.dim_customer_sk        as bill_customer_dk,
  dbcd.dim_customer_demo_sk  as bill_cdemo_dk,
  dbh.dim_household_sk       as bill_hdemo_dk,
  dba.dim_address_sk         as bill_addr_dk,
  dsc.dim_customer_sk        as ship_customer_dk,
  dscd.dim_customer_demo_sk  as ship_cdemo_dk,
  dshh.dim_household_sk      as ship_hdemo_dk,
  dsa.dim_address_sk         as ship_addr_dk,
  dcc.dim_call_center_sk     as call_center_dk,
  dcp.dim_catalog_page_sk    as catalog_page_dk,
  dsm.dim_ship_mode_sk       as ship_mode_dk,
  dw.dim_warehouse_sk        as warehouse_dk,
  di.dim_item_sk             as item_dk,
  dp.dim_promotion_sk        as promo_dk

from base b
left join {{ ref('dim_date') }}          dds  on b.cs_sold_date_sk     = dds.d_date_sk
left join {{ ref('dim_time') }}          dts  on b.cs_sold_time_sk     = dts.t_time_sk
left join {{ ref('dim_date') }}          dsh  on b.cs_ship_date_sk     = dsh.d_date_sk
left join {{ ref('dim_customer') }}      dbc  on b.cs_bill_customer_sk = dbc.c_customer_sk
left join {{ ref('dim_customer_demo') }} dbcd on b.cs_bill_cdemo_sk    = dbcd.cd_demo_sk
left join {{ ref('dim_household') }}     dbh  on b.cs_bill_hdemo_sk    = dbh.hd_demo_sk
left join {{ ref('dim_address') }}       dba  on b.cs_bill_addr_sk     = dba.ca_address_sk
left join {{ ref('dim_customer') }}      dsc  on b.cs_ship_customer_sk = dsc.c_customer_sk
left join {{ ref('dim_customer_demo') }} dscd on b.cs_ship_cdemo_sk    = dscd.cd_demo_sk
left join {{ ref('dim_household') }}     dshh on b.cs_ship_hdemo_sk    = dshh.hd_demo_sk
left join {{ ref('dim_address') }}       dsa  on b.cs_ship_addr_sk     = dsa.ca_address_sk
left join {{ ref('dim_call_center') }}   dcc  on b.cs_call_center_sk   = dcc.cc_call_center_sk
left join {{ ref('dim_catalog_page') }}  dcp  on b.cs_catalog_page_sk  = dcp.cp_catalog_page_sk
left join {{ ref('dim_ship_mode') }}     dsm  on b.cs_ship_mode_sk     = dsm.sm_ship_mode_sk
left join {{ ref('dim_warehouse') }}     dw   on b.cs_warehouse_sk     = dw.w_warehouse_sk
left join {{ ref('dim_item') }}          di   on b.cs_item_sk          = di.i_item_sk
left join {{ ref('dim_promotion') }}     dp   on b.cs_promo_sk         = dp.p_promo_sk
