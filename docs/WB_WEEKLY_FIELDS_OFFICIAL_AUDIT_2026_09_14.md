# Wildberries weekly realization — official 92-field audit

Status: REVIEWED / fail-closed
Audit date: 2026-09-14
Dataset: `wb_weekly_finance_main`
Physical archive schema audited: 92 columns

## Sources used

Primary authority:

1. Wildberries Seller Help — «Детализация еженедельного отчёта реализации».
2. Wildberries Seller Help — «Фиксация тарифов: тарифы на поставку и на остаток».
3. Wildberries API — Financial reports and accounting, current v1 camelCase response schema.
4. Canonical archive header observed in `wb_weekly_finance_main` on 2026-09-14.

The Seller Help article is the business-semantics authority where it gives a column explanation. The API schema is used to confirm physical field names/types and to detect provider schema drift. Archive observation proves what is physically present in our canonical dataset; it does not by itself approve business meaning.

## Audit result

All 92 physical columns in the current canonical archive remain catalogued in `core/semantic_registry.yaml`.

The audit found and corrected material semantic wording in four areas:

- `dlvPrc` — fixed warehouse coefficient that was valid when the supply was planned. It must not be described as proof of the coefficient actually used after the fixation period expires.
- `isKgvpV2` — historical percentage change of WB remuneration coefficient caused by participation in a reduced-commission promotion. In current reports this historical column is normally zero.
- `paymentSchedule` — monetary commission for the «Вывести сейчас» / one-time change of payout term service; it is not a schedule/dimension field.
- cashback/loyalty money fields — `cashbackAmount`, `cashbackDiscount`, and `cashbackCommissionChange` are kept as distinct business amounts and must not be collapsed into one generic cashback metric.

The current WB API response schema also contains `agencyVat`, while the observed canonical archive contract audited here has 92 columns and does not contain it. `agencyVat` is therefore **not approved for archive semantic execution**. A provider field appearing in the API is not automatically added to the Semantic Core: archive presence, meaning, formula/safe use, tests, and coverage behavior must be reviewed first.

## 92/92 field coverage matrix

Evidence labels:

- `HELP+API` — business meaning supported by Seller Help and physical field supported by WB API/archive mapping.
- `HELP` — business meaning supported directly by Seller Help; physical archive field already observed.
- `API+ARCHIVE` — physical field confirmed, but business use remains intentionally narrow/technical or legacy.

| # | Field | Evidence | Approved semantic class |
|---:|---|---|---|
| 1 | reportId | API+ARCHIVE | report identifier |
| 2 | dateFrom | API+ARCHIVE | report-period date |
| 3 | dateTo | API+ARCHIVE | report-period date |
| 4 | createDate | API+ARCHIVE | report creation date |
| 5 | currency | API+ARCHIVE | currency dimension |
| 6 | reportType | API+ARCHIVE | report type |
| 7 | rrdId | API+ARCHIVE | row identifier / dedup |
| 8 | giId | API+ARCHIVE | supply identifier/context |
| 9 | dlvPrc | HELP+API | fixed coefficient at supply planning |
| 10 | fixTariffDateFrom | HELP+API | fixation start |
| 11 | fixTariffDateTo | HELP+API | fixation end |
| 12 | subjectName | API+ARCHIVE | product category |
| 13 | nmId | API+ARCHIVE | WB article ID |
| 14 | brandName | API+ARCHIVE | brand |
| 15 | vendorCode | API+ARCHIVE | seller article |
| 16 | title | API+ARCHIVE | product title |
| 17 | techSize | API+ARCHIVE | product size |
| 18 | sku | API+ARCHIVE | barcode |
| 19 | docTypeName | HELP+API | sale/return document type |
| 20 | quantity | HELP+API | operation quantity; contextual |
| 21 | retailPrice | HELP+API | seller-discounted retail price |
| 22 | retailAmount | HELP+API | realization/return amount |
| 23 | salePercent | HELP+API | legacy seller-discount field |
| 24 | commissionPercent | HELP+API | WB remuneration coefficient, % |
| 25 | officeName | API+ARCHIVE | warehouse/office context |
| 26 | sellerOperName | HELP+API | payment/financial operation reason |
| 27 | orderDt | HELP+API | order date attached to reported operation only |
| 28 | saleDt | HELP+API | sale/buyout/return/operation date |
| 29 | rrDate | API+ARCHIVE | report settlement attribution date |
| 30 | shkId | API+ARCHIVE | warehouse unit identifier |
| 31 | retailPriceWithDisc | HELP+API | report calculation price |
| 32 | deliveryAmount | HELP+API | forward logistics count |
| 33 | returnAmount | HELP+API | reverse logistics count |
| 34 | deliveryService | HELP+API | logistics service amount |
| 35 | giBoxTypeName | API+ARCHIVE | supply/box type context |
| 36 | productDiscountForReport | HELP+API | legacy discount field |
| 37 | sellerPromo | HELP+API | legacy seller-promo field |
| 38 | spp | HELP+API | WB-funded/platform discount, % |
| 39 | kvwBase | HELP+API | base WB remuneration coefficient without VAT |
| 40 | kvw | HELP+API | final WB remuneration coefficient without VAT |
| 41 | supRatingUp | HELP+API | legacy rating-driven coefficient change |
| 42 | isKgvpV2 | HELP+API | legacy promotion-driven coefficient change |
| 43 | ppvzSalesCommission | HELP+API | WB sales reward before agent services, excl. VAT |
| 44 | forPay | HELP+API | amount accrued to seller for item operation |
| 45 | ppvzReward | HELP+API | pickup-point issue/return service expense |
| 46 | acquiringFee | HELP+API | weekly payment-service/acquiring amount |
| 47 | acquiringPercent | HELP+API | payment-service/acquiring rate |
| 48 | paymentProcessing | HELP+API | payment-service type |
| 49 | acquiringBank | API+ARCHIVE | payment provider/bank context |
| 50 | vw | HELP+API | WB reward excluding VAT |
| 51 | vwNds | HELP+API | VAT on WB reward |
| 52 | ppvzOfficeName | API+ARCHIVE | pickup-point name/address context |
| 53 | ppvzOfficeId | API+ARCHIVE | pickup-point identifier |
| 54 | ppvzSupplierName | API+ARCHIVE | legacy pickup-point partner name |
| 55 | ppvzSupplierInn | API+ARCHIVE | legacy pickup-point partner TIN |
| 56 | declarationNumber | API+ARCHIVE | customs declaration identifier |
| 57 | stickerId | API+ARCHIVE | FBS sticker identifier |
| 58 | country | API+ARCHIVE | operation country context |
| 59 | srvDbs | HELP+API | paid DBS/EDBS delivery flag |
| 60 | penalty | HELP+API | penalty amount |
| 61 | additionalPayment | HELP+API | WB reward adjustment |
| 62 | rebillLogisticCost | HELP+API | rebilled logistics/transport cost |
| 63 | paidStorage | HELP+API | paid storage charge |
| 64 | deduction | HELP+API | deduction amount |
| 65 | paidAcceptance | HELP+API | paid acceptance/processing charge |
| 66 | orderId | API+ARCHIVE | assembly-task identifier; not universal order ID |
| 67 | isB2b | HELP+API | legal-entity sale flag |
| 68 | trbxId | HELP+API | grouped-order box identifier |
| 69 | installmentCofinancingAmount | HELP+API | installment co-financing discount amount |
| 70 | wibesDiscountPercent | HELP+API | Wibes discount, % |
| 71 | cashbackAmount | HELP+API | amount withheld/returned for awarded loyalty points |
| 72 | cashbackDiscount | HELP+API | loyalty-discount compensation used to pay for goods |
| 73 | cashbackCommissionChange | HELP+API | participation cost/commission for seller cashback |
| 74 | paymentSchedule | HELP+API | commission for one-time payout-term change («Вывести сейчас») |
| 75 | deliveryMethod | HELP+API | historical logistics fulfillment/model observation |
| 76 | sellerPromoId | HELP+API | seller promotion ID |
| 77 | sellerPromoDiscount | HELP+API | seller promotion discount, % |
| 78 | loyaltyId | HELP+API | loyalty-program ID |
| 79 | loyaltyDiscount | HELP+API | seller loyalty discount, % |
| 80 | uuidPromocode | API+ARCHIVE | promo-code identifier |
| 81 | salePricePromocodeDiscountPrc | API+ARCHIVE | promo-code discount, % |
| 82 | articleSubstitution | API+ARCHIVE | substitution article identifier |
| 83 | salePriceAffiliatedDiscountPrc | API+ARCHIVE | affiliated/substitution discount, % |
| 84 | salePriceWholesaleDiscountPrc | API+ARCHIVE | wholesale/B2B discount, % |
| 85 | b2bCustomerTin | API+ARCHIVE | B2B buyer TIN |
| 86 | paidWithSocialCertificate | API+ARCHIVE | social-certificate payment flag |
| 87 | warehouseLogisticsCoeff | API+ARCHIVE | logistics coefficient recorded on operation |
| 88 | orderUid | HELP+API | basket/order transaction identifier |
| 89 | srid | API+ARCHIVE | WB order/operation-chain identifier |
| 90 | rebillLogisticOrg | API+ARCHIVE | rebilled logistics organization |
| 91 | bonusTypeName | HELP+API | logistics/fine/adjustment explanation |
| 92 | kiz | API+ARCHIVE | mandatory marking code |

## Non-negotiable execution guardrails

- `orderDt`/`orderUid` never become the complete orders metric.
- `deliveryMethod` and tariff fields describe historical observations only.
- `dlvPrc` does not prove the coefficient actually charged after its fixation period.
- percentages are never silently summed as money.
- sale and return calculations use explicit operation buckets; signs are not guessed.
- currencies are never combined.
- an API field absent from the approved archive schema is not executable merely because the provider started returning it.
- any new provider column requires a schema/semantic review before business execution.
