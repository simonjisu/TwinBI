{{ config(materialized='table') }}

with base as (
  select *
  from {{ ref('stg_store_returns') }}
)

select
  -- native keys
  b.sr_returned_date_sk,
  b.sr_return_time_sk,
  b.sr_item_sk,
  b.sr_customer_sk,
  b.sr_cdemo_sk,
  b.sr_hdemo_sk,
  b.sr_addr_sk,
  b.sr_store_sk,
  b.sr_reason_sk,
  -- measures
  b.sr_ticket_number,
  b.sr_return_quantity,
  b.sr_return_amt,
  b.sr_return_tax,
  b.sr_return_amt_inc_tax,
  b.sr_fee,
  b.sr_return_ship_cost,
  b.sr_refunded_cash,
  b.sr_reversed_charge,
  b.sr_store_credit,
  b.sr_net_loss,
  -- surrogate keys for joins
  dd.dim_date_sk           as returned_date_dk,
  dt.dim_time_sk           as return_time_dk,
  di.dim_item_sk           as item_dk,
  dc.dim_customer_sk       as customer_dk,
  dcd.dim_customer_demo_sk as cdemo_dk,
  dh.dim_household_sk      as hdemo_dk,
  da.dim_address_sk        as addr_dk,
  ds.dim_store_sk          as store_dk,
  dr.dim_reason_sk         as reason_dk

from base b
left join {{ ref('dim_date') }}          dd  on b.sr_returned_date_sk = dd.d_date_sk
left join {{ ref('dim_time') }}          dt  on b.sr_return_time_sk   = dt.t_time_sk
left join {{ ref('dim_item') }}          di  on b.sr_item_sk          = di.i_item_sk
left join {{ ref('dim_customer') }}      dc  on b.sr_customer_sk      = dc.c_customer_sk
left join {{ ref('dim_customer_demo') }} dcd on b.sr_cdemo_sk         = dcd.cd_demo_sk
left join {{ ref('dim_household') }}     dh  on b.sr_hdemo_sk         = dh.hd_demo_sk
left join {{ ref('dim_address') }}       da  on b.sr_addr_sk          = da.ca_address_sk
left join {{ ref('dim_store') }}         ds  on b.sr_store_sk         = ds.s_store_sk
left join {{ ref('dim_reason') }}        dr  on b.sr_reason_sk        = dr.r_reason_sk
