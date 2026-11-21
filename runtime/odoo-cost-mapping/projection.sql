SELECT line.id::integer AS invoice_line_id, move.id::integer AS invoice_id,
       date_trunc('month',move.invoice_date)::date AS invoice_month,
       company.name::text AS company, currency.name::text AS currency,
       product.default_code::text AS product, 'ea'::text AS unit,
       line.quantity::numeric AS quantity, -line.balance AS revenue,
       cost.balance AS cogs, -line.balance-cost.balance AS gross_profit
FROM account_move_line line
JOIN account_move move ON move.id=line.move_id
JOIN res_company company ON company.id=move.company_id
JOIN res_currency currency ON currency.id=company.currency_id
JOIN product_product product ON product.id=line.product_id
JOIN account_move_line cost ON cost.cogs_origin_id=line.id
    AND cost.move_id=move.id AND cost.account_id=company.expense_account_id
    AND cost.display_type='cogs'
WHERE company.id=1 AND company.name='한빛상품' AND currency.name='KRW'
  AND move.state='posted' AND move.move_type='out_invoice'
  AND move.currency_id=company.currency_id AND line.company_id=company.id
  AND line.currency_id=company.currency_id AND cost.company_id=company.id
  AND cost.currency_id=company.currency_id AND line.display_type='product'
  AND move.invoice_date>=DATE '2026-01-01'
  AND (date_trunc('month',move.invoice_date)+INTERVAL '1 month - 1 day')::date<=company.hard_lock_date;
