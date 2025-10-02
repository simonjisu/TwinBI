
You are an expert in OLAP. Assuming you have a cube of a denormalized table in a data warehouse, given an sql in TPC-DS below, your task is to transform it in to a series of OLAP operations like roll-up, roll-down, slide, dice. You can ignore any join and union operations as the table is denormalized. Please return a series of operations in JSON format.

**Query**
-- start query 31 in stream 0 using template query31.tpl 
WITH ss 
     AS (SELECT ca_county, 
                d_qoy, 
                d_year, 
                Sum(ss_ext_sales_price) AS store_sales 
         FROM   store_sales, 
                date_dim, 
                customer_address 
         WHERE  ss_sold_date_sk = d_date_sk 
                AND ss_addr_sk = ca_address_sk 
         GROUP  BY ca_county, 
                   d_qoy, 
                   d_year), 
     ws 
     AS (SELECT ca_county, 
                d_qoy, 
                d_year, 
                Sum(ws_ext_sales_price) AS web_sales 
         FROM   web_sales, 
                date_dim, 
                customer_address 
         WHERE  ws_sold_date_sk = d_date_sk 
                AND ws_bill_addr_sk = ca_address_sk 
         GROUP  BY ca_county, 
                   d_qoy, 
                   d_year) 
SELECT ss1.ca_county, 
       ss1.d_year, 
       ws2.web_sales / ws1.web_sales     web_q1_q2_increase, 
       ss2.store_sales / ss1.store_sales store_q1_q2_increase, 
       ws3.web_sales / ws2.web_sales     web_q2_q3_increase, 
       ss3.store_sales / ss2.store_sales store_q2_q3_increase 
FROM   ss ss1, 
       ss ss2, 
       ss ss3, 
       ws ws1, 
       ws ws2, 
       ws ws3 
WHERE  ss1.d_qoy = 1 
       AND ss1.d_year = 2001 
       AND ss1.ca_county = ss2.ca_county 
       AND ss2.d_qoy = 2 
       AND ss2.d_year = 2001 
       AND ss2.ca_county = ss3.ca_county 
       AND ss3.d_qoy = 3 
       AND ss3.d_year = 2001 
       AND ss1.ca_county = ws1.ca_county 
       AND ws1.d_qoy = 1 
       AND ws1.d_year = 2001 
       AND ws1.ca_county = ws2.ca_county 
       AND ws2.d_qoy = 2 
       AND ws2.d_year = 2001 
       AND ws1.ca_county = ws3.ca_county 
       AND ws3.d_qoy = 3 
       AND ws3.d_year = 2001 
       AND CASE 
             WHEN ws1.web_sales > 0 THEN ws2.web_sales / ws1.web_sales 
             ELSE NULL 
           END > CASE 
                   WHEN ss1.store_sales > 0 THEN 
                   ss2.store_sales / ss1.store_sales 
                   ELSE NULL 
                 END 
       AND CASE 
             WHEN ws2.web_sales > 0 THEN ws3.web_sales / ws2.web_sales 
             ELSE NULL 
           END > CASE 
                   WHEN ss2.store_sales > 0 THEN 
                   ss3.store_sales / ss2.store_sales 
                   ELSE NULL 
                 END 
ORDER  BY ss1.d_year; 


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
