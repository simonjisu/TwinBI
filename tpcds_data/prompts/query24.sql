
You are an expert in OLAP. Assuming you have a cube of a denormalized table in a data warehouse, given an sql in TPC-DS below, your task is to transform it in to a series of OLAP operations like roll-up, roll-down, slide, dice. You can ignore any join and union operations as the table is denormalized. Please return a series of operations in JSON format.

**Query**
-- start query 24 in stream 0 using template query24.tpl 
WITH ssales 
     AS (SELECT c_last_name, 
                c_first_name, 
                s_store_name, 
                ca_state, 
                s_state, 
                i_color, 
                i_current_price, 
                i_manager_id, 
                i_units, 
                i_size, 
                Sum(ss_net_profit) netpaid 
         FROM   store_sales, 
                store_returns, 
                store, 
                item, 
                customer, 
                customer_address 
         WHERE  ss_ticket_number = sr_ticket_number 
                AND ss_item_sk = sr_item_sk 
                AND ss_customer_sk = c_customer_sk 
                AND ss_item_sk = i_item_sk 
                AND ss_store_sk = s_store_sk 
                AND c_birth_country = Upper(ca_country) 
                AND s_zip = ca_zip 
                AND s_market_id = 6 
         GROUP  BY c_last_name, 
                   c_first_name, 
                   s_store_name, 
                   ca_state, 
                   s_state, 
                   i_color, 
                   i_current_price, 
                   i_manager_id, 
                   i_units, 
                   i_size) 
SELECT c_last_name, 
       c_first_name, 
       s_store_name, 
       Sum(netpaid) paid 
FROM   ssales 
WHERE  i_color = 'papaya' 
GROUP  BY c_last_name, 
          c_first_name, 
          s_store_name 
HAVING Sum(netpaid) > (SELECT 0.05 * Avg(netpaid) 
                       FROM   ssales); 

WITH ssales 
     AS (SELECT c_last_name, 
                c_first_name, 
                s_store_name, 
                ca_state, 
                s_state, 
                i_color, 
                i_current_price, 
                i_manager_id, 
                i_units, 
                i_size, 
                Sum(ss_net_profit) netpaid 
         FROM   store_sales, 
                store_returns, 
                store, 
                item, 
                customer, 
                customer_address 
         WHERE  ss_ticket_number = sr_ticket_number 
                AND ss_item_sk = sr_item_sk 
                AND ss_customer_sk = c_customer_sk 
                AND ss_item_sk = i_item_sk 
                AND ss_store_sk = s_store_sk 
                AND c_birth_country = Upper(ca_country) 
                AND s_zip = ca_zip 
                AND s_market_id = 6 
         GROUP  BY c_last_name, 
                   c_first_name, 
                   s_store_name, 
                   ca_state, 
                   s_state, 
                   i_color, 
                   i_current_price, 
                   i_manager_id, 
                   i_units, 
                   i_size) 
SELECT c_last_name, 
       c_first_name, 
       s_store_name, 
       Sum(netpaid) paid 
FROM   ssales 
WHERE  i_color = 'chartreuse' 
GROUP  BY c_last_name, 
          c_first_name, 
          s_store_name 
HAVING Sum(netpaid) > (SELECT 0.05 * Avg(netpaid) 
                       FROM   ssales); 


**Response format**
[
  {
    "operation": "slice",
    "dimension": "date",
    "filter": {
      "column": "d_year",
      "operator": "equals_to",
      "value": 2001
    }
  },
  ...
]
