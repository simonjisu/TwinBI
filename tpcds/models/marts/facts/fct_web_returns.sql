{{ config(materialized='table') }}

with base as (
  select *
  from {{ ref('stg_web_returns') }}
)

select
  -- native keys
  b.wr_returned_date_sk,
  b.wr_returned_time_sk,
  b.wr_item_sk,
  b.wr_refunded_customer_sk,
  b.wr_refunded_cdemo_sk,
  b.wr_refunded_hdemo_sk,
  b.wr_refunded_addr_sk,
  b.wr_returning_customer_sk,
  b.wr_returning_cdemo_sk,
  b.wr_returning_hdemo_sk,
  b.wr_returning_addr_sk,
  b.wr_web_page_sk,
  b.wr_reason_sk,
  -- measures
  b.wr_order_number,
  b.wr_return_quantity,
  b.wr_return_amt,
  b.wr_return_tax,
  b.wr_return_amt_inc_tax,
  b.wr_fee,
  b.wr_return_ship_cost,
  b.wr_refunded_cash,
  b.wr_reversed_charge,
  b.wr_account_credit,
  b.wr_net_loss,
  -- surrogate keys for joins
  dd.dim_date_sk            as returned_date_dk,
  dt.dim_time_sk            as returned_time_dk,
  di.dim_item_sk            as item_dk,
  drc.dim_customer_sk       as refunded_customer_dk,
  drcd.dim_customer_demo_sk as refunded_cdemo_dk,
  drh.dim_household_sk      as refunded_hdemo_dk,
  dra.dim_address_sk        as refunded_addr_dk,
  drc2.dim_customer_sk      as returning_customer_dk,
  drcd2.dim_customer_demo_sk as returning_cdemo_dk,
  drh2.dim_household_sk     as returning_hdemo_dk,
  dra2.dim_address_sk       as returning_addr_dk,
  dwp.dim_web_page_sk       as web_page_dk,
  drr.dim_reason_sk         as reason_dk

from base b
left join {{ ref('dim_date') }}          dd    on b.wr_returned_date_sk   = dd.date_sk
left join {{ ref('dim_time') }}          dt    on b.wr_returned_time_sk   = dt.time_sk
left join {{ ref('dim_item') }}          di    on b.wr_item_sk            = di.item_sk
left join {{ ref('dim_customer') }}      drc   on b.wr_refunded_customer_sk  = drc.customer_sk
left join {{ ref('dim_customer_demo') }} drcd  on b.wr_refunded_cdemo_sk     = drcd.customer_demo_sk
left join {{ ref('dim_household') }}     drh   on b.wr_refunded_hdemo_sk     = drh.household_demo_sk
left join {{ ref('dim_address') }}       dra   on b.wr_refunded_addr_sk      = dra.address_sk
left join {{ ref('dim_customer') }}      drc2  on b.wr_returning_customer_sk = drc2.customer_sk
left join {{ ref('dim_customer_demo') }} drcd2 on b.wr_returning_cdemo_sk    = drcd2.customer_demo_sk
left join {{ ref('dim_household') }}     drh2  on b.wr_returning_hdemo_sk    = drh2.household_demo_sk
left join {{ ref('dim_address') }}       dra2  on b.wr_returning_addr_sk     = dra2.address_sk
left join {{ ref('dim_web_page') }}      dwp   on b.wr_web_page_sk           = dwp.web_page_sk
left join {{ ref('dim_reason') }}        drr   on b.wr_reason_sk             = drr.reason_sk

