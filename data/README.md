# Cambridge road-safety data pack

Downloaded and inspected 18 September 2026. Data are public reported personal-injury road collisions, not all road incidents. This pack contains Cambridge extracts and an unchanged source workbook. It is not a route-risk model.

## What is included

- `cambridgeshire_local.xlsx`: original county source, unchanged. Crashes: 10,649 rows × 29 columns; Vehicles: 20,132 × 23; Casualties: 14,579 × 16. The workbook includes its own Query Info and FAQs.
- `local_cambridge_*_2017_2026H1.csv`: Cambridge City only, as assigned by police: 2,430 collisions, 4,622 vehicles, 2,741 casualties. Dates 2017–30 June 2026. Locations, junctions and other categories are readable text. Coordinates are British National Grid eastings/northings, not longitude/latitude.
- `national_cambridge_*_2025.csv`: final national 2025 release, Cambridge ONS district E07000008: 215 collisions, 416 vehicles, 249 casualties. The legacy numeric local_authority_district field is -1 throughout this subset; use the ONS field.
- `national_cambridge_*_2025_decoded.csv`: same 2025 records with selected labels added from the official dictionary. Original codes remain alongside them.
- `national_cambridge_*_2021_2025.csv`: five-year national collision and casualty extracts: 1,187 collisions and 1,348 casualties. These overlap the 2025 files: do not concatenate them without deduplication. Five-year vehicle records were not downloaded.
- `data_guide_2025.xlsx`: original official national code dictionary. -1 commonly means missing/unknown; 0 often has a valid meaning. Historic and current fields are not interchangeable. Consult the guide for each field.
- `field_inventory.csv`: actual fields and blank/-1 counts in each supplied CSV. A blank or -1 may mean not applicable; these counts are not a universal quality score.
- `checks.py`, `Cambridge_Data_Check.ipynb`, `checks_results.json`: executable checks, captured outputs, and results. Run `python checks.py` after unzipping, with pandas installed.
- `local_profile.json`, `national_profile.json`: original detailed profiles. `source_comparison.json`: focused reconciliation. `downloads.json`: original URLs, byte counts and SHA-256 hashes, including large national source files omitted from this compact pack. `pack_manifest.json`: hashes of packaged files.

## Freshness and joins

The local workbook was prepared 7 September 2026, covers 1 January 2017–30 June 2026, labels 2025 and 2026 provisional, and warns that its latest two months may be incomplete. Its geography is Cambridgeshire excluding Peterborough. Cambridge is a subset, not the county.

Local tables join on Collision Reference No.; vehicle and casualty records require their own reference as well for a unique row. National tables join on collision_index; collision_ref_no is not unique across years. Every supplied Cambridge collision/vehicle/casualty key was checked, and child-table row counts reconcile for each collision in both the local and national 2025 subsets.

Local 2025 has 216 Cambridge collisions versus national 215. All 215 national records have inferred matching local identifiers and matching dates/times; 214 also have identical coordinates. Arbury Road reference 1555522 differs in coordinates by about 42 metres. Local reference 1693079 at Cherry Hinton Road/Clifton Road has no matched national identifier. This describes the discrepancy; its cause is not established. Do not combine release totals or deduplicate only by coordinates. The inferred bridge was verified for this subset only.

## Concrete findings

In final national Cambridge 2025, 249 casualties include 121 cyclists, 32 pedestrians and 15 people with known age under 16. Nine casualty ages are unknown. Fifty-five casualties were serious and none fatal. Fifteen known under-16 casualties is a subset, not an additional category to add to the travel-mode counts.

All 215 national Cambridge collisions have coordinates, dates, times, injury severity and speed limit. Crossing type is unknown/missing in 28; junction detail is missing in 30. Driver journey purpose is unknown/not requested for 243 of 416 vehicle records. Broad education/escort labels cannot establish a school journey.

The local file has street/junction text and vehicle manoeuvres. Example records: Vicarage Terrace/St Matthews Street, 13 April 2026, serious collision, two casualties (provisional); Sturton Street/New Street, 15 February 2026, slight collision, one casualty (provisional); York Street/New Street, 5 June 2022, slight collision, two casualties. Collision severity is the most severe injury category in the collision, not necessarily the severity of every casualty. These examples were selected for local relevance, not as a ranking of hazardous streets.

## Fitness for Max's prototype

Suitable: map recorded injury collisions, inspect people/vehicles involved, show dated evidence around a junction, and prepare questions for a site inspection or council request.

Not supplied: near misses, pedestrian/cyclist traffic volumes, current photographs or street geometry, a verified school-route network, school identity or a reliable school-journey flag, or established causes of each collision. Therefore raw counts cannot establish the safest route, individual journey risk or whether a particular infrastructure change would prevent injuries. No report at a location does not mean no risk. Historic severity comparisons need the official methodological notes.

Sources:
- National release and definitions: https://www.gov.uk/government/statistical-data-sets/road-safety-open-data
- Local dataset: https://data.cambridgeshireinsight.org.uk/dataset/c9e98fe8-6431-4a8d-9792-719d48c8abb9
- County road-safety page: https://www.cambridgeshire.gov.uk/residents/travel-roads-and-parking/roads-and-pathways/road-safety

The original source workbook and data guide retain source documentation. Use the source pages for reuse terms and updated releases. No current field survey, causal assessment or route recommendation was performed.
