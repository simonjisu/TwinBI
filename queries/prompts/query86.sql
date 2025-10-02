
You are an expert in OLAP. Assuming you have a cube of a denormalized table in a data warehouse, given an sql in TPC-DS below, your task is to transform it in to a series of OLAP operations like roll-up, roll-down, slide, dice. You can ignore any join and union operations as the table is denormalized. Please return a series of operations in JSON format.

**Query**
-- start query 86 in stream 0 using template query86.tpl 
SELECT Sum(ws_net_paid)                         AS total_sum, 
               i_category, 
               i_class, 
               Grouping(i_category) + Grouping(i_class) AS lochierarchy, 
               Rank() 
                 OVER ( 
                   partition BY Grouping(i_category)+Grouping(i_class), CASE 
                 WHEN Grouping( 
                 i_class) = 0 THEN i_category END 
                   ORDER BY Sum(ws_net_paid) DESC)      AS rank_within_parent 
FROM   web_sales, 
       date_dim d1, 
       item 
WHERE  d1.d_month_seq BETWEEN 1183 AND 1183 + 11 
       AND d1.d_date_sk = ws_sold_date_sk 
       AND i_item_sk = ws_item_sk 
GROUP  BY rollup( i_category, i_class ) 
ORDER  BY lochierarchy DESC, 
          CASE 
            WHEN lochierarchy = 0 THEN i_category 
          END, 
          rank_within_parent
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
