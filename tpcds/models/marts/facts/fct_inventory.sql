{{ config(materialized='table') }}

with base as (
  select *
  from {{ ref('stg_inventory') }}
)

select
  -- native keys
  b.inv_date_sk,
  b.inv_item_sk,
  b.inv_warehouse_sk,
  -- measures
  b.inv_quantity_on_hand,
  -- surrogate keys for joins
  dd.dim_date_sk       as date_dk,
  di.dim_item_sk       as item_dk,
  dw.dim_warehouse_sk  as warehouse_dk

from base b
left join {{ ref('dim_date') }}      dd on b.inv_date_sk      = dd.date_sk
left join {{ ref('dim_item') }}      di on b.inv_item_sk      = di.item_sk
left join {{ ref('dim_warehouse') }} dw on b.inv_warehouse_sk = dw.warehouse_sk

