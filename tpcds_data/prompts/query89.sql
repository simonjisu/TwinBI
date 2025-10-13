
You are an expert in OLAP. Assuming you have a cube of a denormalized table in a data warehouse, given an sql in TPC-DS below, your task is to transform it in to a series of OLAP operations like roll-up, roll-down, slide, dice. You can ignore any join and union operations as the table is denormalized. Please return a series of operations in JSON format.

**Query**
-- start query 89 in stream 0 using template query89.tpl 
SELECT  * 
FROM  (SELECT i_category, 
              i_class, 
              i_brand, 
              s_store_name, 
              s_company_name, 
              d_moy, 
              Sum(ss_sales_price) sum_sales, 
              Avg(Sum(ss_sales_price)) 
                OVER ( 
                  partition BY i_category, i_brand, s_store_name, s_company_name 
                ) 
                                  avg_monthly_sales 
       FROM   item, 
              store_sales, 
              date_dim, 
              store 
       WHERE  ss_item_sk = i_item_sk 
              AND ss_sold_date_sk = d_date_sk 
              AND ss_store_sk = s_store_sk 
              AND d_year IN ( 2002 ) 
              AND ( ( i_category IN ( 'Home', 'Men', 'Sports' ) 
                      AND i_class IN ( 'paint', 'accessories', 'fitness' ) ) 
                     OR ( i_category IN ( 'Shoes', 'Jewelry', 'Women' ) 
                          AND i_class IN ( 'mens', 'pendants', 'swimwear' ) ) ) 
       GROUP  BY i_category, 
                 i_class, 
                 i_brand, 
                 s_store_name, 
                 s_company_name, 
                 d_moy) tmp1 
WHERE  CASE 
         WHEN ( avg_monthly_sales <> 0 ) THEN ( 
         Abs(sum_sales - avg_monthly_sales) / avg_monthly_sales ) 
         ELSE NULL 
       END > 0.1 
ORDER  BY sum_sales - avg_monthly_sales, 
          s_store_name
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
