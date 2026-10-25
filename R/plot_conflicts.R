library(tidyverse)
library(glue)
source("R/db_connect.R")

gtfs_schema <- "gtfs_20261019"

calendar_range <- as_tibble(DBI::dbGetQuery(con, glue(
"
SELECT min(date) AS first_day, max(date) AS last_day
FROM {gtfs_schema}.service_days;
"
)))

calendar_limits <- c(calendar_range$first_day, calendar_range$last_day)
  
conflicts <- as_tibble(DBI::dbGetQuery(con, glue(
"
SELECT 
f.finding_id, 
f.kind,
f.routes,
CASE 
  WHEN f.kind = 'conflict' 
  THEN CONCAT(f.prev_stop_name, '-', f.stop_name, '-', f.next_stop_name)
  ELSE f.stop_name
END AS stops,
pd.date, 
pd.pattern_id, 
pd.n_trips
FROM {gtfs_schema}.map_check_findings f
JOIN {gtfs_schema}.pattern_days pd USING (pattern_id)
")))


conflicts |> 
  ggplot() +
  geom_point(aes(date, stops, size = n_trips)) +
  labs(x = "Date", y = "Stops") +
  scale_x_date(
    date_breaks = "1 week",
    date_minor_breaks = "1 day",
    limits = as_date(calendar_limits)) +
  scale_size_area("Number of trips", max_size = 2, guide = "legend") +
  facet_grid(rows = vars(kind, routes), scales = "free_y", space = "free_y") +
  theme(
    axis.text.x = element_text(angle = 45, vjust = 1, hjust = 1),
    strip.text.y = element_text(angle = 0)
  )

ggsave(glue("output/conflicts_{gtfs_schema}.png"), unit = "cm", width = 35, height = 20, dpi = 300) # adjust dims if necessary
