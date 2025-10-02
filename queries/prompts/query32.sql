
You are an expert in OLAP. Assuming you have a cube of a denormalized table in a data warehouse, given an sql in TPC-DS below, your task is to transform it in to a series of OLAP operations like roll-up, roll-down, slide, dice. You can ignore any join and union operations as the table is denormalized. Please return a series of operations in JSON format.

**Query**
-- start query 32 in stream 0 using template query32.tpl 
SELECT 
       Sum(cs_ext_discount_amt) AS `excess discount amount`
FROM   catalog_sales , 
       item , 
       date_dim 
WHERE  i_manufact_id = 610 
AND    i_item_sk = cs_item_sk 
AND    d_date BETWEEN '2001-03-04' AND    ( 
              Cast('2001-03-04' AS DATE) + INTERVAL '90' day) 
AND    d_date_sk = cs_sold_date_sk 
AND    cs_ext_discount_amt > 
       ( 
              SELECT 1.3 * avg(cs_ext_discount_amt) 
              FROM   catalog_sales , 
                     date_dim 
              WHERE  cs_item_sk = i_item_sk 
              AND    d_date BETWEEN '2001-03-04' AND    ( 
                            cast('2001-03-04' AS date) + INTERVAL '90' day) 
              AND    d_date_sk = cs_sold_date_sk ) 
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
