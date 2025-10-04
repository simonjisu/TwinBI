{{ config(materialized='table') }}

with base as (
  select *
  from {{ ref('stg_catalog_returns') }}
)

select
  -- native keys
  b.cr_returned_date_sk,
  b.cr_returned_time_sk,
  b.cr_item_sk,
  b.cr_refunded_customer_sk,
  b.cr_refunded_cdemo_sk,
  b.cr_refunded_hdemo_sk,
  b.cr_refunded_addr_sk,
  b.cr_returning_customer_sk,
  b.cr_returning_cdemo_sk,
  b.cr_returning_hdemo_sk,
  b.cr_returning_addr_sk,
  b.cr_call_center_sk,
  b.cr_catalog_page_sk,
  b.cr_ship_mode_sk,
  b.cr_warehouse_sk,
  b.cr_reason_sk,
  -- measures
  b.cr_order_number,
  b.cr_return_quantity,
  b.cr_return_amount,
  b.cr_return_tax,
  b.cr_return_amt_inc_tax,
  b.cr_fee,
  b.cr_return_ship_cost,
  b.cr_refunded_cash,
  b.cr_reversed_charge,
  b.cr_store_credit,
  b.cr_net_loss,
  -- surrogate keys for joins
  dd.dim_date_sk            as returned_date_dk,
  dt.dim_time_sk            as returned_time_dk,
  di.dim_item_sk            as item_dk,
  dcrc.dim_customer_sk      as refunded_customer_dk,
  drcd.dim_customer_demo_sk as refunded_cdemo_dk,
  drh.dim_household_sk      as refunded_hdemo_dk,
  dra.dim_address_sk        as refunded_addr_dk,
  drrc.dim_customer_sk      as returning_customer_dk,
  drrcd.dim_customer_demo_sk as returning_cdemo_dk,
  drrh.dim_household_sk     as returning_hdemo_dk,
  drra.dim_address_sk       as returning_addr_dk,
  dcc.dim_call_center_sk    as call_center_dk,
  dcp.dim_catalog_page_sk   as catalog_page_dk,
  dsm.dim_ship_mode_sk      as ship_mode_dk,
  dw.dim_warehouse_sk       as warehouse_dk,
  dr.dim_reason_sk          as reason_dk

from base b
left join {{ ref('dim_date') }}          dd    on b.cr_returned_date_sk   = dd.d_date_sk
left join {{ ref('dim_time') }}          dt    on b.cr_returned_time_sk   = dt.t_time_sk
left join {{ ref('dim_item') }}          di    on b.cr_item_sk            = di.i_item_sk
left join {{ ref('dim_customer') }}      dcrc  on b.cr_refunded_customer_sk  = dcrc.c_customer_sk
left join {{ ref('dim_customer_demo') }} drcd  on b.cr_refunded_cdemo_sk     = drcd.cd_demo_sk
left join {{ ref('dim_household') }}     drh   on b.cr_refunded_hdemo_sk     = drh.hd_demo_sk
left join {{ ref('dim_address') }}       dra   on b.cr_refunded_addr_sk      = dra.ca_address_sk
left join {{ ref('dim_customer') }}      drrc  on b.cr_returning_customer_sk = drrc.c_customer_sk
left join {{ ref('dim_customer_demo') }} drrcd on b.cr_returning_cdemo_sk    = drrcd.cd_demo_sk
left join {{ ref('dim_household') }}     drrh  on b.cr_returning_hdemo_sk    = drrh.hd_demo_sk
left join {{ ref('dim_address') }}       drra  on b.cr_returning_addr_sk     = drra.ca_address_sk
left join {{ ref('dim_call_center') }}   dcc   on b.cr_call_center_sk        = dcc.cc_call_center_sk
left join {{ ref('dim_catalog_page') }}  dcp   on b.cr_catalog_page_sk       = dcp.cp_catalog_page_sk
left join {{ ref('dim_ship_mode') }}     dsm   on b.cr_ship_mode_sk          = dsm.sm_ship_mode_sk
left join {{ ref('dim_warehouse') }}     dw    on b.cr_warehouse_sk          = dw.w_warehouse_sk
left join {{ ref('dim_reason') }}        dr    on b.cr_reason_sk             = dr.r_reason_sk
