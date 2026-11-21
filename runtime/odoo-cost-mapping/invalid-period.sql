-- This exact proof requires receipts, bills, deliveries and invoices in the same
-- closed month. Open/future periods are not represented as zero-cost results.
WITH company AS (SELECT id,currency_id,hard_lock_date,expense_account_id,account_stock_valuation_id FROM res_company WHERE id=1),
period_moves AS (
    SELECT m.id,m.company_id,m.currency_id,m.state,m.move_type,m.date,m.invoice_date FROM account_move m, company c
    WHERE m.date>=DATE '2026-01-01' AND m.date<=c.hard_lock_date
), invoice_lines AS (
    SELECT l.id,l.move_id,l.company_id,l.currency_id,l.quantity,l.balance,l.product_id,l.product_uom_id,l.purchase_line_id,
           m.move_type,m.invoice_date,m.date AS accounting_date
    FROM account_move_line l JOIN period_moves m ON m.id=l.move_id
    WHERE m.move_type IN ('in_invoice','out_invoice') AND l.display_type='product'
), stock AS (
    SELECT s.id,s.state,s.quantity,s.product_uom,s.product_id,s.purchase_line_id,s.sale_line_id,date_trunc('month',s.date)::date AS month,
           src.usage AS source_usage,dst.usage AS destination_usage
    FROM stock_move s LEFT JOIN stock_location src ON src.id=s.location_id
    LEFT JOIN stock_location dst ON dst.id=s.location_dest_id, company c
    WHERE s.date>=TIMESTAMP '2026-01-01' AND s.date<(c.hard_lock_date+1)::timestamp
), quantities AS (
    SELECT product_id,date_trunc('month',invoice_date)::date AS month,move_type,
           CASE WHEN move_type='in_invoice' THEN purchase_line_id
             ELSE (SELECT min(r.order_line_id) FROM sale_order_line_invoice_rel r WHERE r.invoice_line_id=l.id) END AS order_line,
           quantity AS accounted,0::numeric AS transferred
    FROM invoice_lines l
    UNION ALL
    SELECT product_id,month,CASE WHEN source_usage='supplier' THEN 'in_invoice' ELSE 'out_invoice' END,
           CASE WHEN source_usage='supplier' THEN purchase_line_id ELSE sale_line_id END,0,quantity
    FROM stock WHERE state='done'
), invalid AS (
    SELECT 'document' AS reason FROM period_moves m,company c
    WHERE m.state<>'posted' OR m.move_type IN ('in_refund','out_refund')
       OR m.currency_id<>c.currency_id OR m.company_id<>c.id
    UNION ALL
    SELECT 'date' FROM invoice_lines WHERE invoice_date IS NULL
       OR date_trunc('month',invoice_date)<>date_trunc('month',accounting_date)
    UNION ALL
    SELECT 'missing_invoice_binding' FROM invoice_lines l WHERE product_id IS NULL
       OR product_uom_id IS NULL OR currency_id IS NULL OR quantity IS NULL OR balance IS NULL OR company_id IS NULL
       OR (move_type='in_invoice' AND purchase_line_id IS NULL)
       OR (move_type='out_invoice' AND (SELECT count(*) FROM sale_order_line_invoice_rel r WHERE r.invoice_line_id=l.id)<>1)
       OR NOT EXISTS (SELECT 1 FROM product_product p JOIN product_template t ON t.id=p.product_tmpl_id
           JOIN product_category cat ON cat.id=t.categ_id WHERE p.id=l.product_id)
    UNION ALL
    SELECT 'product_policy' FROM invoice_lines l
    JOIN product_product p ON p.id=l.product_id
    JOIN product_template t ON t.id=p.product_tmpl_id
    JOIN product_category cat ON cat.id=t.categ_id,company c
    WHERE l.quantity<=0 OR l.company_id<>c.id OR l.currency_id<>c.currency_id
       OR p.default_code NOT IN ('A','B') OR t.company_id IS DISTINCT FROM c.id
       OR NOT t.is_storable OR t.uom_id<>1 OR l.product_uom_id<>1
       -- The exact live ir.default inventory is checked before this query.
       OR COALESCE(cat.property_cost_method->>'1','fifo')<>'fifo'
       OR COALESCE(cat.property_valuation->>'1','real_time')<>'real_time'
       OR COALESCE((t.property_account_expense_id->>'1')::int,
          (cat.property_account_expense_categ_id->>'1')::int,c.expense_account_id)<>c.expense_account_id
       OR COALESCE((cat.property_stock_valuation_account_id->>'1')::int,
          c.account_stock_valuation_id)<>c.account_stock_valuation_id
    UNION ALL
    SELECT 'cogs_lineage' FROM invoice_lines l,company c
    WHERE l.move_type='out_invoice' AND (
       SELECT count(*)<>2 OR count(*) FILTER (WHERE cost.account_id=c.expense_account_id)<>1
          OR count(*) FILTER (WHERE cost.account_id=c.account_stock_valuation_id)<>1
          OR sum(cost.balance)<>0
          OR bool_or(cost.move_id<>l.move_id OR cost.company_id<>c.id OR cost.currency_id<>c.currency_id
             OR cost.balance IS NULL OR cost.quantity IS NULL OR cost.product_id IS NULL
             OR cost.product_uom_id IS NULL OR cost.currency_id IS NULL OR cost.company_id IS NULL
             OR cost.quantity<>l.quantity OR cost.product_id<>l.product_id OR cost.product_uom_id<>l.product_uom_id)
       FROM account_move_line cost WHERE cost.cogs_origin_id=l.id AND cost.display_type='cogs')
    UNION ALL
    SELECT 'stock' FROM stock WHERE state<>'done' OR quantity<=0 OR product_uom IS DISTINCT FROM 1
       OR product_id IS NULL OR quantity IS NULL OR source_usage IS NULL OR destination_usage IS NULL
       OR (source_usage='supplier' AND purchase_line_id IS NULL)
       OR (destination_usage='customer' AND sale_line_id IS NULL)
       OR NOT ((source_usage='supplier' AND destination_usage='internal')
           OR (source_usage='internal' AND destination_usage='customer'))
    UNION ALL
    SELECT 'unbilled_or_undelivered' FROM quantities GROUP BY product_id,month,move_type,order_line
    HAVING order_line IS NULL OR COALESCE(sum(accounted),0)<>COALESCE(sum(transferred),0)
    UNION ALL
    SELECT 'unbalanced_document' FROM account_move_line l JOIN period_moves m ON m.id=l.move_id
    GROUP BY l.move_id HAVING sum(l.balance)<>0
)
SELECT reason FROM invalid LIMIT 1;
