"Kaupo " + .period + ": runs \(.totals.num_runs) (\(.totals.active_runs) active" +
(if (.totals.market_closed_runs != null and .totals.inactive_runs != null) then
  ", \(.totals.market_closed_runs) market closed, \(.totals.inactive_runs) inactive)"
 else ")"
 end) + ", fills \(.totals.total_fills)" +
(if .totals.by_currency == null then "; currency totals unavailable"
 else (.totals.by_currency | to_entries | sort_by(.key) | map("; P&L \(.value.total_pnl) \(.key), fees \(.value.total_fees) \(.key)") | join("")) +
      (if (.totals.unclassified_runs // 0) > 0 then "; currency unavailable for \(.totals.unclassified_runs) active runs" else "" end)
 end)
