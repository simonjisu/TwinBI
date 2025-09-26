{% macro sk(cols) -%}
  {{ dbt_utils.generate_surrogate_key(cols) }}
{%- endmacro %}