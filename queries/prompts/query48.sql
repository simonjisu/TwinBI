
You are an expert in OLAP. Assuming you have a cube of a denormalized table in a data warehouse, given an sql in TPC-DS below, your task is to transform it in to a series of OLAP operations like roll-up, roll-down, slide, dice. You can ignore any join and union operations as the table is denormalized. Please return a series of operations in JSON format.

**Query**
-- start query 48 in stream 0 using template query48.tpl 
SELECT Sum (ss_quantity) 
FROM   store_sales, 
       store, 
       customer_demographics, 
       customer_address, 
       date_dim 
WHERE  s_store_sk = ss_store_sk 
       AND ss_sold_date_sk = d_date_sk 
       AND d_year = 1999 
       AND ( ( cd_demo_sk = ss_cdemo_sk 
               AND cd_marital_status = 'W' 
               AND cd_education_status = 'Secondary' 
               AND ss_sales_price BETWEEN 100.00 AND 150.00 ) 
              OR ( cd_demo_sk = ss_cdemo_sk 
                   AND cd_marital_status = 'M' 
                   AND cd_education_status = 'Advanced Degree' 
                   AND ss_sales_price BETWEEN 50.00 AND 100.00 ) 
              OR ( cd_demo_sk = ss_cdemo_sk 
                   AND cd_marital_status = 'D' 
                   AND cd_education_status = '2 yr Degree' 
                   AND ss_sales_price BETWEEN 150.00 AND 200.00 ) ) 
       AND ( ( ss_addr_sk = ca_address_sk 
               AND ca_country = 'United States' 
               AND ca_state IN ( 'TX', 'NE', 'MO' ) 
               AND ss_net_profit BETWEEN 0 AND 2000 ) 
              OR ( ss_addr_sk = ca_address_sk 
                   AND ca_country = 'United States' 
                   AND ca_state IN ( 'CO', 'TN', 'ND' ) 
                   AND ss_net_profit BETWEEN 150 AND 3000 ) 
              OR ( ss_addr_sk = ca_address_sk 
                   AND ca_country = 'United States' 
                   AND ca_state IN ( 'OK', 'PA', 'CA' ) 
                   AND ss_net_profit BETWEEN 50 AND 25000 ) ); 


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
