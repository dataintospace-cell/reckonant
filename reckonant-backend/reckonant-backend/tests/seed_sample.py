"""
Fills an empty copy of the Signals Feeder Engine schema (tests/schema.sql) with a small set of
FICTIONAL companies, so the API and dashboard can be tried without the real database.
Company numbers start with 99 (not used by Companies House); every name is made up.

    psql "$RK_DATABASE_URL" -f tests/schema.sql
    python3 tests/seed_sample.py "$RK_DATABASE_URL"
"""
import sys
from datetime import date, timedelta

import psycopg

T = date.today()
d = lambda days: T - timedelta(days=days)          # days ago
f = lambda days: T + timedelta(days=days)          # days ahead
txt = lambda dd: dd.strftime("%d/%m/%Y")           # CQC files store dates as text

COMPANIES = [
    # num, name, status, type, created, sic, premises, line1, locality, region, postcode
    ("99000001", "BRIGHTWATER CARE HOMES LIMITED", "active", "ltd", d(4200), ["87100"], "12", "Harbour Road", "Plymouth", "Devon", "PL1 2AB"),
    ("99000002", "NORTHFIELD HOME SUPPORT LTD", "active", "ltd", d(2600), ["88100"], "4", "Mill Lane", "Leeds", "West Yorkshire", "LS1 4DT"),
    ("99000003", "KESTREL FACILITIES SERVICES LIMITED", "active", "ltd", d(5100), ["81210"], "Unit 7", "Kestrel Park", "Coventry", "", "CV1 3HX"),
    ("99000004", "ASHCROFT LIVING LIMITED", "liquidation", "ltd", d(3300), ["87300"], "2", "Station Road", "Manchester", "", "M1 1AA"),
    ("99000005", "MERIDIAN NURSING AGENCY LTD", "active", "ltd", d(1500), ["78200"], "88", "High Street", "Croydon", "Surrey", "CR0 1NX"),
    ("99000006", "OAKHURST DOMICILIARY CARE LTD", "active", "ltd", d(250), ["88100"], "5", "Church Street", "Norwich", "Norfolk", "NR2 1AA"),
    ("99000007", "PENNINE DIGITAL HEALTH LIMITED", "active", "ltd", d(2900), ["62012"], "Floor 3", "Albert Square", "Manchester", "", "M2 5DB"),
    ("99000008", "CAVENDISH RESIDENTIAL CARE LIMITED", "active", "ltd", d(6100), ["87100"], "1", "Cavendish Place", "Brighton", "East Sussex", "BN1 2AA"),
    ("99000009", "THORNBURY CLEANING CONTRACTORS LTD", "active", "ltd", d(3900), ["81220"], "15", "Bridge Street", "Cardiff", "", "CF10 1AA"),
    ("99000010", "LARKHILL SUPPORTED LIVING CIC", "active", "community-interest-company", d(1900), ["87200"], "3", "Larkhill Road", "Glasgow", "", "G1 1AA"),
]
OFFICERS = [  # num, key, name, role, appointed, resigned, nationality
    ("99000001", "o1", "HARPER, Jane Elizabeth", "director", d(3000), None, "British"),
    ("99000001", "o2", "OKAFOR, Daniel", "director", d(40), None, "British"),
    ("99000002", "o3", "NOWAK, Anna", "director", d(2500), None, "Polish"),
    ("99000002", "o4", "REILLY, Thomas", "director", d(1800), d(60), "Irish"),
    ("99000003", "o5", "FOSTER, Grace", "director", d(4000), None, "British"),
    ("99000004", "o6", "TURNER, Michael", "director", d(3000), None, "British"),
    ("99000005", "o7", "SHARMA, Priya", "director", d(1400), None, "British"),
    ("99000006", "o8", "KHAN, Aisha", "director", d(250), None, "British"),
    ("99000007", "o9", "ELLISON, George", "director", d(2000), None, "British"),
    ("99000008", "o10", "CLARKE, Emma", "director", d(5000), None, "British"),
    ("99000009", "o11", "HUGHES, Oliver", "director", d(3500), None, "British"),
    ("99000010", "o12", "MBEKI, Amara", "director", d(1800), None, "British"),
]
PSCS = [  # num, key, name, kind, notified, ceased_on, natures
    ("99000001", "p1", "Jane Elizabeth Harper", "individual-person-with-significant-control", d(3000), None, ["ownership-of-shares-75-to-100-percent"]),
    ("99000003", "p2", "KESTREL GROUP HOLDINGS LIMITED", "corporate-entity-person-with-significant-control", d(20), None, ["ownership-of-shares-75-to-100-percent", "voting-rights-75-to-100-percent"]),
    ("99000005", "p3", "Priya Sharma", "individual-person-with-significant-control", d(1400), None, ["ownership-of-shares-50-to-75-percent"]),
]
CHANGES = [  # num, type, detail, date
    ("99000003", "registered_office_changed", "Registered office moved to Unit 7, Kestrel Park, Coventry", d(12)),
    ("99000004", "status_changed", "Status changed from active to liquidation", d(25)),
]
CQC_LOC = [  # location_id, name, care_home, beds, rating, published, start, city, postcode, provider_id, provider_name, web, ch_number
    ("1-1001", "Brightwater House", "Y", "42", "Good", txt(d(300)), txt(d(3000)), "Plymouth", "PL1 2AB", "1-P01", "Brightwater Care Homes Limited", "www.brightwatercare.example", "99000001"),
    ("1-1002", "Brightwater Lodge", "Y", "30", "Good", txt(d(50)), txt(d(20)), "Exeter", "EX1 1AA", "1-P01", "Brightwater Care Homes Limited", "www.brightwatercare.example", "99000001"),
    ("1-2001", "Northfield Home Support - Leeds", "N", "", "Requires improvement", txt(d(120)), txt(d(2500)), "Leeds", "LS1 4DT", "1-P02", "Northfield Home Support Ltd", "", "99000002"),
    ("1-4001", "Ashcroft Court", "Y", "28", "Inadequate", txt(d(70)), txt(d(3200)), "Manchester", "M1 1AA", "1-P04", "Ashcroft Living Limited", "", "99000004"),
    ("1-6001", "Oakhurst Care Norwich", "N", "", "Not yet rated", "", txt(d(200)), "Norwich", "NR2 1AA", "1-P06", "Oakhurst Domiciliary Care Ltd", "", "99000006"),
    ("1-8001", "Cavendish Gardens", "Y", "64", "Outstanding", txt(d(40)), txt(d(6000)), "Brighton", "BN1 2AA", "1-P08", "Cavendish Residential Care Limited", "cavendishcare.example", "99000008"),
    ("1-8002", "Cavendish Mews", "Y", "38", "Good", txt(d(400)), txt(d(5000)), "Hove", "BN3 1AA", "1-P08", "Cavendish Residential Care Limited", "cavendishcare.example", "99000008 "),
    ("1-1101", "Larkhill House", "Y", "12", "Good", txt(d(500)), txt(d(1800)), "Glasgow", "G1 1AA", "1-P10", "Larkhill Supported Living CIC", "", ""),
]
INSOLVENCY = [  # notice_id, published, kind, code, type_label, num, title
    (5001, d(25), "creditors-voluntary-liquidation", "2410", "Resolutions for Winding-up", "99000004", "ASHCROFT LIVING LIMITED"),
    (5002, d(26), "creditors-voluntary-liquidation", "2432", "Appointment of Liquidators", "99000004", "ASHCROFT LIVING LIMITED"),
]


def seed(dsn: str):
    with psycopg.connect(dsn, autocommit=True) as c:
        x = c.execute
        for r in COMPANIES:
            x('INSERT INTO "6_company_master".company (company_number, company_name, company_status, company_type, date_of_creation, '
              'sic_codes, ro_premises, ro_address_line_1, ro_locality, ro_region, ro_postal_code, ro_country, fetch_status, first_seen_at, '
              'last_fetched_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())', [*r, "England", "ok"])
        for num, *_ in COMPANIES:
            x('INSERT INTO "6_company_master".company_source VALUES (%s,%s,now())', [num, "COM-01"])
        for r in OFFICERS:
            x('INSERT INTO "6_company_master".company_officer (company_number, officer_key, officer_name, officer_role, is_director, '
              'appointed_on, resigned_on, nationality, first_seen_at, last_seen_at) VALUES (%s,%s,%s,%s,true,%s,%s,%s,now(),now())', r)
        for num, key, name, kind, notified, ceased_on, nat in PSCS:
            x('INSERT INTO "6_company_master".company_psc (company_number, psc_key, psc_name, psc_kind, notified_on, ceased_on, ceased, '
              'natures_of_control, first_seen_at, last_seen_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),now())',
              [num, key, name, kind, notified, ceased_on, ceased_on is not None, nat])
        for i, (num, t, detail, dd) in enumerate(CHANGES):
            x('INSERT INTO "6_company_master".company_change_log VALUES (%s,%s,%s,%s,%s,now(),%s)', [i + 1, num, "", t, detail, dd])

        # CQC current locations (newest monthly file) + one month-to-month diff + closures
        for r in CQC_LOC:
            lid, name, ch, beds, rating, pub, start, city, pc, pid, pname, web, chn = r
            x('INSERT INTO "2_care_directory_with_filters"."02_november_2026_hsca_active_locations" (location_id, location_name, care_home, '
              'care_homes_beds, location_latest_overall_rating, publication_date, location_hsca_start_date, location_city, '
              'location_postal_code, provider_id, provider_name, provider_web_address, provider_companies_house_number, dormant_y_n, '
              'location_type_sector) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
              [lid, name, ch, beds, rating, pub, start, city, pc, pid, pname, web, chn, "N", "Social Care Org"])
            if chn:
                x('INSERT INTO "2_care_directory_with_filters"."01_october_2026_hsca_active_locations" (location_id, location_name, '
                  'provider_companies_house_number) VALUES (%s,%s,%s)', [lid, name, chn])
        diff = '"2_care_directory_with_filters"."diff_01_october_2026_02_november_2026_hsca_active_locations"'
        x(f'INSERT INTO {diff} (change_type, key_value, location_name_curr, care_homes_beds_curr, location_city_curr, '
          'provider_companies_house_number_curr) VALUES (%s,%s,%s,%s,%s,%s)', ["added", "1-1002", "Brightwater Lodge", "30", "Exeter", "99000001"])
        x(f'INSERT INTO {diff} (change_type, key_value, changed_columns, location_name_curr, location_city_curr, '
          'location_latest_overall_rating_prev, location_latest_overall_rating_curr, provider_companies_house_number_curr) '
          'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)', ["changed", "1-8001", "location_latest_overall_rating", "Cavendish Gardens", "Brighton",
                                              "Good", "Outstanding", "99000008"])
        x(f'INSERT INTO {diff} (change_type, key_value, changed_columns, location_name_curr, location_city_curr, '
          'location_latest_overall_rating_prev, location_latest_overall_rating_curr, provider_companies_house_number_curr) '
          'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)', ["changed", "1-4001", "location_latest_overall_rating", "Ashcroft Court", "Manchester",
                                              "Requires improvement", "Inadequate", "99000004"])
        x('INSERT INTO "4_deactivated_locations"."01_october_2026_deactivated_locations" (location_id, location_name, '
          'location_hsca_end_date, care_homes_beds_at_point_location_de_activated, location_city, provider_companies_house_number) '
          'VALUES (%s,%s,%s,%s,%s,%s)', ["1-2002", "Northfield Home Support - Bradford", txt(d(45)), "", "Bradford", "99000002"])

        # Link by register_link: Larkhill's CQC provider has no company number in the CQC file
        x('INSERT INTO "6_company_master".register_link VALUES (%s,%s,%s,%s,now())', ["CQC-02", "1-P10", "99000010", "name+postcode"])

        # Gazette
        for nid, pub, kind, code, label, num, title in INSOLVENCY:
            x('INSERT INTO "5_insolvency".v_insolvency_notice (notice_id, published_date, insolvency_kind, notice_code, type_label, '
              'company_number, title, url) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
              [nid, pub, kind, code, label, num, title, f"https://www.thegazette.co.uk/notice/{nid}"])
        x('INSERT INTO "6_company_master".person_link (source_code, source_ref, person_name, notice_date, company_number, officer_key, '
          'officer_name, officer_role, match_level, linked_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())',
          ["INS-01", "7001", "Oliver Hughes", d(80), "99000009", "o11", "HUGHES, Oliver", "director", "name+postcode+dob"])

        # HMRC defaulter + Home Office sponsors (both linked by register_link)
        x('INSERT INTO "10_hmrc_tax_defaulters".v_defaulter VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
          [301, d(150), d(20), True, "Deliberate defaulters", "Thornbury Cleaning Contractors Ltd", "Cleaning", "Cardiff",
           "01/2023 to 12/2024", "£184,220", "£73,688", "https://www.gov.uk/government/publications/publishing-details-of-deliberate-tax-defaulters-pddd"])
        x('INSERT INTO "6_company_master".register_link VALUES (%s,%s,%s,%s,now())', ["HMD-01", "301", "99000009", "name+postcode"])
        x('INSERT INTO "11_sponsor_register".v_sponsor VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
          [401, d(30), d(1), True, "Meridian Nursing Agency Ltd", "Croydon", "Surrey", "Worker (A rating)", "Skilled Worker", ""])
        x('INSERT INTO "6_company_master".register_link VALUES (%s,%s,%s,%s,now())', ["HOS-01", "401", "99000005", "name+town"])
        x('INSERT INTO "11_sponsor_register".v_sponsor VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
          [402, d(900), d(10), False, "Northfield Home Support Ltd", "Leeds", "", "Worker (A rating)", "Skilled Worker", ""])
        x('INSERT INTO "6_company_master".register_link VALUES (%s,%s,%s,%s,now())', ["HOS-01", "402", "99000002", "name+town"])
        x('INSERT INTO "11_sponsor_register".change_log VALUES (%s,%s,%s,%s,%s,%s)', [1, d(10), "removed", 402, "Northfield Home Support Ltd",
                                                                                     "Removed from the register of licensed sponsors"])

        # Public contracts (Contracts Finder + Find a Tender) and payments
        def contract(code, rid, title, buyer, value, awarded, start, end, num, sup):
            s = {"CON-01": "12_contracts_finder", "FTS-01": "13_find_a_tender"}[code]
            x(f'INSERT INTO "{s}".notice ("Release ID", "OCID", "Title", "Buyer", "Stage", "Awarded value", "url") VALUES (%s,%s,%s,%s,%s,%s,%s)',
              [rid, "ocds-" + rid, title, buyer, "award", value, f"https://www.contractsfinder.service.gov.uk/notice/{rid}"])
            if code == "FTS-01":
                x(f'INSERT INTO "{s}".v_notice ("Release ID", "OCID", "Title", "Buyer", "Stage", "Awarded value", "url") VALUES (%s,%s,%s,%s,%s,%s,%s)',
                  [rid, "ocds-" + rid, title, buyer, "award", value, f"https://www.find-tender.service.gov.uk/Notice/{rid}"])
            x(f'INSERT INTO "{s}".supplier (supplier_row_id, "Release ID", "OCID", "Supplier name", company_number, "Award value", '
              '"Currency", "Award date", "Contract start", "Contract end") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
              [abs(hash(rid)) % 10**9, rid, "ocds-" + rid, sup, num, value, "GBP", awarded, start, end])
        contract("CON-01", "CF-1", "Facilities management for council offices", "Coventry City Council", 1250000, d(18), d(10), f(720), "99000003", "Kestrel Facilities Services Ltd")
        contract("CON-01", "CF-2", "Office cleaning services", "Cardiff Council", 310000, d(800), d(790), f(75), "99000009", "Thornbury Cleaning Contractors")
        contract("FTS-01", "FT-1", "Electronic care records system", "NHS Greater Manchester ICB", 2400000, d(55), d(40), f(1100), "99000007", "Pennine Digital Health Ltd")
        contract("FTS-01", "FT-2", "Domiciliary care framework - Lot 3", "Norfolk County Council", 900000, d(700), d(690), f(150), " 99000006 ", "Oakhurst Domiciliary Care")
        x('INSERT INTO "14_payments_transparency".payment ("Payment ID", "Title", "Contracting authority", "Supplier", "Payment value", '
          '"Payment date", company_number) VALUES (%s,%s,%s,%s,%s,%s,%s)', ["PAY-1", "Monthly FM fee", "Coventry City Council",
                                                                            "Kestrel Facilities", 104000, d(9), "99000003"])
        # Open tenders
        for rid, title, buyer, value, closes, region in [
            ("FT-9", "Home care services 2027-2031", "Leeds City Council", 4200000, f(21), "Yorkshire and the Humber"),
            ("FT-8", "Nursing home placements framework", "Kent County Council", 12000000, f(35), "South East"),
        ]:
            x('INSERT INTO "13_find_a_tender".v_notice ("Release ID", "Title", "Buyer", "Value", "Tender end", "Region", "Stage", '
              '"Open opportunity", "Published", "url") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)',
              [rid, title, buyer, value, closes, region, "tender", "true", f"https://www.find-tender.service.gov.uk/Notice/{rid}"])
    print(f"Seeded {len(COMPANIES)} fictional companies")


if __name__ == "__main__":
    seed(sys.argv[1])


