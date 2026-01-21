Schema Explorer Tools

Overview
These tools expose the SchemaExplorer graph for the star schema. Use them when
the user asks about fact tables, dimensions, measures, attributes, or whether
an attribute value exists.

Tools
- get_facts
  Returns the list of fact tables.
  Output: ["fact_sales", ...] or {"error": "..."}

- get_schema_info
  Returns schema nodes for a fact table.
  Input: fact_table (str)
  Output: list of nodes with:
  - name, type ("fact" or "dimension")
  - attributes (for dimensions)
  - measures and fks (for the fact table)

- search_attribute
  Searches hierarchy paths that lead to the attribute.
  Input: fact_table (str), attribute_name (str)
  Output: list of results with:
  - dimension, attribute
  - path: steps from dimension to attribute
  - stats: count, distinct_count, min/max, dtype, unique_values metadata

- search_value_exists
  Checks whether an attribute value exists.
  Input: fact_table (str), attribute_name (str), value (str)
  Output: true/false or {"error": "..."}

Typical usage patterns
- "What fact tables exist?" -> get_facts
- "What dimensions are in fact_sales?" -> get_schema_info("fact_sales")
- "Where is the year attribute?" -> search_attribute("fact_sales", "year")
- "Does year=2024 exist?" -> search_value_exists("fact_sales", "year", "2024")

Notes
- The explorer is configured to use the star schema under ./data/sales.
- Attribute lookup is case-insensitive for labels; use short attribute names.
