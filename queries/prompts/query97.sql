
You are an expert in OLAP. Assuming you have a cube of a denormalized table in a data warehouse, given an sql in TPC-DS below, your task is to transform it in to a series of OLAP operations like roll-up, roll-down, slide, dice. You can ignore any join and union operations as the table is denormalized. Please return a series of operations in JSON format.

**Query**

-- start query 97 in stream 0 using template query97.tpl 
WITH ssci 
     AS (SELECT ss_customer_sk customer_sk, 
                ss_item_sk     item_sk 
         FROM   store_sales, 
                date_dim 
         WHERE  ss_sold_date_sk = d_date_sk 
                AND d_month_seq BETWEEN 1196 AND 1196 + 11 
         GROUP  BY ss_customer_sk, 
                   ss_item_sk), 
     csci 
     AS (SELECT cs_bill_customer_sk customer_sk, 
                cs_item_sk          item_sk 
         FROM   catalog_sales, 
                date_dim 
         WHERE  cs_sold_date_sk = d_date_sk 
                AND d_month_seq BETWEEN 1196 AND 1196 + 11 
         GROUP  BY cs_bill_customer_sk, 
                   cs_item_sk) 
SELECT Sum(CASE 
                     WHEN ssci.customer_sk IS NOT NULL 
                          AND csci.customer_sk IS NULL THEN 1 
                     ELSE 0 
                   END) store_only, 
               Sum(CASE 
                     WHEN ssci.customer_sk IS NULL 
                          AND csci.customer_sk IS NOT NULL THEN 1 
                     ELSE 0 
                   END) catalog_only, 
               Sum(CASE 
                     WHEN ssci.customer_sk IS NOT NULL 
                          AND csci.customer_sk IS NOT NULL THEN 1 
                     ELSE 0 
                   END) store_and_catalog 
FROM   ssci 
       FULL OUTER JOIN csci 
                    ON ( ssci.customer_sk = csci.customer_sk 
                         AND ssci.item_sk = csci.item_sk )
LIMIT 100; 


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
