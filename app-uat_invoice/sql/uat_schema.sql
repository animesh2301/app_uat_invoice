/* ===========================================================================
   UAT - REQUIRED. Objects in the bc schema only; nothing in dbo is touched.
   Run once against the Azure SQL database.
   =========================================================================== */

/* ---------------------------------------------------------------------------
   1. Two new landing tables.

   The ContinuFlow mappings look values up from bc.stg_paymentMethods
   (Mst_Vendors.PAYMENT_METHOD_CODE) and bc.stg_items
   (AP_DC_PurchaseOrderLines.ItemNumber / ItemType / Category). Neither entity
   is in the CF doc, so both still land in bc.stg_*. Neither was in the
   mirror before, so without these those columns would be NULL.

   Everything is NVARCHAR to match the existing bc.stg_* convention.
   --------------------------------------------------------------------------- */

IF OBJECT_ID('bc.stg_paymentMethods', 'U') IS NULL
CREATE TABLE bc.stg_paymentMethods
(
    id                   NVARCHAR(100) NULL,
    code                 NVARCHAR(100) NULL,
    displayName          NVARCHAR(200) NULL,
    lastModifiedDateTime NVARCHAR(100) NULL,
    company              NVARCHAR(200) NULL
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_stg_paymentMethods_id')
CREATE INDEX IX_stg_paymentMethods_id ON bc.stg_paymentMethods (id);
GO


IF OBJECT_ID('bc.stg_items', 'U') IS NULL
CREATE TABLE bc.stg_items
(
    id                             NVARCHAR(100) NULL,
    number                         NVARCHAR(100) NULL,
    displayName                    NVARCHAR(200) NULL,
    type                           NVARCHAR(100) NULL,
    itemCategoryId                 NVARCHAR(100) NULL,
    itemCategoryCode               NVARCHAR(100) NULL,
    blocked                        NVARCHAR(50)  NULL,
    baseUnitOfMeasureId            NVARCHAR(100) NULL,
    baseUnitOfMeasureCode          NVARCHAR(100) NULL,
    gtin                           NVARCHAR(100) NULL,
    inventory                      NVARCHAR(100) NULL,
    unitPrice                      NVARCHAR(100) NULL,
    priceIncludesTax               NVARCHAR(50)  NULL,
    unitCost                       NVARCHAR(100) NULL,
    taxGroupId                     NVARCHAR(100) NULL,
    taxGroupCode                   NVARCHAR(100) NULL,
    generalProductPostingGroupId   NVARCHAR(100) NULL,
    generalProductPostingGroupCode NVARCHAR(100) NULL,
    inventoryPostingGroupId        NVARCHAR(100) NULL,
    inventoryPostingGroupCode      NVARCHAR(100) NULL,
    lastModifiedDateTime           NVARCHAR(100) NULL,
    company                        NVARCHAR(200) NULL
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_stg_items_id')
CREATE INDEX IX_stg_items_id ON bc.stg_items (id);
GO


/* ---------------------------------------------------------------------------
   2. Invoice posting audit log.

   ERP_ERROR on AP_DataConversions holds the latest error so the ContinuFlow
   UI surfaces it, but it is one column - it cannot hold the history of
   several attempts. This table is append-only: one row per step per attempt.

   It is also what makes retries safe. BcInvoiceId is written the moment the
   BC invoice header exists, so a retry after a mid-way failure resumes that
   invoice instead of creating a second one.
   --------------------------------------------------------------------------- */

IF OBJECT_ID('bc.InvoicePostLog', 'U') IS NULL
CREATE TABLE bc.InvoicePostLog
(
    LogId           BIGINT IDENTITY(1,1) PRIMARY KEY,
    RecordId        BIGINT        NOT NULL,   -- AP_DataConversions.RECORD_ID
    ClientId        INT           NULL,
    CompanyId       INT           NULL,
    Attempt         INT           NOT NULL,
    Step            VARCHAR(60)   NOT NULL,
    Outcome         VARCHAR(20)   NOT NULL,   -- ok | skipped | held | error
    Detail          NVARCHAR(MAX) NULL,
    BcInvoiceId     VARCHAR(50)   NULL,
    BcInvoiceNumber VARCHAR(50)   NULL,
    LoggedAt        DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_InvoicePostLog_Record')
CREATE INDEX IX_InvoicePostLog_Record
    ON bc.InvoicePostLog (RecordId, LogId);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_InvoicePostLog_Outcome')
CREATE INDEX IX_InvoicePostLog_Outcome
    ON bc.InvoicePostLog (Outcome, LoggedAt);
GO


/* ---------------------------------------------------------------------------
   3. BC id -> ContinuFlow key map.

   A BC delete event carries only the BC id, and Mst_Vendors / mst_store have
   no BC id column. Every vendor and location upsert records its pair here,
   so a later delete can find the row (vendor delete -> isActive = 0).

   Populate it once after deploying: run CfBackfill for vendors and
   locations. Until then, deletes fall back to the old bc.stg_vendors /
   bc.stg_locations rows.
   --------------------------------------------------------------------------- */

IF OBJECT_ID('bc.CfKeyMap', 'U') IS NULL
CREATE TABLE bc.CfKeyMap
(
    Entity    VARCHAR(50)   NOT NULL,   -- vendors | locations
    CompanyId INT           NOT NULL,   -- ContinuFlow COMPANY_ID
    BcId      VARCHAR(50)   NOT NULL,   -- BC systemId
    CfKey     NVARCHAR(255) NOT NULL,   -- BP_CODE / PROFIT_CENTER_CODE
    UpdatedAt DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_CfKeyMap PRIMARY KEY (Entity, CompanyId, BcId)
);
GO


/* ===========================================================================
   Checks worth running before UAT
   =========================================================================== */

/* Does every pending GL line's PROFIT_CENTER_ID resolve to a store in the
   same company? Any row returned here would hold its invoice. */
-- SELECT g.RECORD_ID, g.SERIAL_NO, g.PROFIT_CENTER_ID,
--        dc.COMPANY_ID AS invoice_company, s.COMPANY_ID AS store_company,
--        s.PROFIT_CENTER_CODE AS division
-- FROM dbo.AP_DataConversions dc
-- JOIN dbo.AP_DC_GL_Details g ON g.RECORD_ID = dc.RECORD_ID
-- LEFT JOIN dbo.mst_store s ON s.PROFIT_CENTER_ID = g.PROFIT_CENTER_ID
-- WHERE dc.CLIENT_ID = 363 AND dc.COMPANY_ID IN (889, 890)
--   AND dc.STATUS = 'APPROVED' AND dc.ERP_POST = 'N'
--   AND g.PROFIT_CENTER_ID IS NOT NULL
--   AND (s.PROFIT_CENTER_ID IS NULL OR s.COMPANY_ID <> dc.COMPANY_ID);

/* Do the GL codes on pending invoices resolve to a BC account?
   Any row returned here would hold its invoice. */
-- SELECT DISTINCT g.GLCODE, dc.COMPANY_ID
-- FROM dbo.AP_DataConversions dc
-- JOIN dbo.AP_DC_GL_Details g ON g.RECORD_ID = dc.RECORD_ID
-- LEFT JOIN dbo.COA_Master_SAP coa
--        ON coa.CLIENT_ID = dc.CLIENT_ID
--       AND coa.COMPANY_ID = dc.COMPANY_ID
--       AND coa.ACCOUNT_CODE = g.GLCODE
-- WHERE dc.CLIENT_ID = 363 AND dc.COMPANY_ID IN (889, 890)
--   AND dc.STATUS = 'APPROVED' AND dc.ERP_POST = 'N'
--   AND coa.ACCOUNT_NUMBER IS NULL;

/* Duplicate keys left by the historical load. Run before creating the
   indexes above; each row returned is a key the direct load will UPDATE
   more than one row for. */
-- SELECT 'Mst_Vendors' AS tbl, CLIENT_ID, COMPANY_ID, BP_CODE AS k, COUNT(*) n
-- FROM dbo.Mst_Vendors WHERE CLIENT_ID = 363
-- GROUP BY CLIENT_ID, COMPANY_ID, BP_CODE HAVING COUNT(*) > 1
-- UNION ALL
-- SELECT 'mst_store', CLIENT_ID, COMPANY_ID, PROFIT_CENTER_CODE, COUNT(*)
-- FROM dbo.mst_store WHERE CLIENT_ID = 363
-- GROUP BY CLIENT_ID, COMPANY_ID, PROFIT_CENTER_CODE HAVING COUNT(*) > 1
-- UNION ALL
-- SELECT 'AP_DC_PurchaseOrderLines', CLIENT_ID, COMPANY_ID, POInternalID, COUNT(*)
-- FROM dbo.AP_DC_PurchaseOrderLines WHERE CLIENT_ID = 363
-- GROUP BY CLIENT_ID, COMPANY_ID, POInternalID HAVING COUNT(*) > 1
-- UNION ALL
-- SELECT 'COA_Master_SAP', CLIENT_ID, COMPANY_ID, ACCOUNT_NUMBER, COUNT(*)
-- FROM dbo.COA_Master_SAP WHERE CLIENT_ID = 363
-- GROUP BY CLIENT_ID, COMPANY_ID, ACCOUNT_NUMBER HAVING COUNT(*) > 1;

/* mst_store rows duplicated per branch. The doc's initial load LEFT JOINed
   mst_branch on company_id, so a company with several branches got one store
   row per branch. The direct load keeps one row per store code. */
-- SELECT COMPANY_ID, PROFIT_CENTER_CODE, COUNT(DISTINCT BRANCH_ID) AS branches
-- FROM dbo.mst_store WHERE CLIENT_ID = 363
-- GROUP BY COMPANY_ID, PROFIT_CENTER_CODE HAVING COUNT(DISTINCT BRANCH_ID) > 1;
