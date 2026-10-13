/* ===========================================================================
   OPTIONAL - indexes on the ContinuFlow dbo tables, for the CF team.

   The app does NOT need these to work correctly. Without them, each write
   scans the table to find its row instead of seeking it. Locking is
   unaffected either way: the app serialises writers with sp_getapplock on
   the record's key, which takes no table locks.

   Whether these are worth asking for depends on table size. Check row
   counts first:

     SELECT 'Mst_Vendors' t, COUNT_BIG(*) n FROM dbo.Mst_Vendors UNION ALL
     SELECT 'mst_store', COUNT_BIG(*) FROM dbo.mst_store UNION ALL
     SELECT 'COA_Master_SAP', COUNT_BIG(*) FROM dbo.COA_Master_SAP UNION ALL
     SELECT 'AP_DC_PurchaseOrder', COUNT_BIG(*) FROM dbo.AP_DC_PurchaseOrder UNION ALL
     SELECT 'AP_DC_PurchaseOrderLines', COUNT_BIG(*) FROM dbo.AP_DC_PurchaseOrderLines UNION ALL
     SELECT 'AP_DC_PurchaseReceipts', COUNT_BIG(*) FROM dbo.AP_DC_PurchaseReceipts UNION ALL
     SELECT 'AP_DC_PurchaseReceiptLines', COUNT_BIG(*) FROM dbo.AP_DC_PurchaseReceiptLines UNION ALL
     SELECT 'AP_DataConversions', COUNT_BIG(*) FROM dbo.AP_DataConversions;

   Rough guide: tens of thousands of rows scan in milliseconds - don't
   bother. Hundreds of thousands and up, ask for the index on that table.
   AP_DC_PurchaseOrderLines is the one most likely to need it: a PO with
   40 lines does 40 lookups.

   Non-unique on purpose: if earlier loads left duplicate keys, a unique
   index would fail to create.
   =========================================================================== */

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Mst_Vendors_Key')
CREATE INDEX IX_Mst_Vendors_Key
    ON dbo.Mst_Vendors (CLIENT_ID, COMPANY_ID, BP_CODE);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Mst_Store_Key')
CREATE INDEX IX_Mst_Store_Key
    ON dbo.mst_store (CLIENT_ID, COMPANY_ID, PROFIT_CENTER_CODE);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_AP_DC_PO_Key')
CREATE INDEX IX_AP_DC_PO_Key
    ON dbo.AP_DC_PurchaseOrder (CompanyCode, POInternalID);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_AP_DC_POLines_Key')
CREATE INDEX IX_AP_DC_POLines_Key
    ON dbo.AP_DC_PurchaseOrderLines (CLIENT_ID, COMPANY_ID, POInternalID);
GO

-- removing lines that were deleted from the PO in BC
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_AP_DC_POLines_PO')
CREATE INDEX IX_AP_DC_POLines_PO
    ON dbo.AP_DC_PurchaseOrderLines (CLIENT_ID, COMPANY_ID, PONumber);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_AP_DC_Receipts_Key')
CREATE INDEX IX_AP_DC_Receipts_Key
    ON dbo.AP_DC_PurchaseReceipts (CLIENT_ID, COMPANY_ID, id);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_AP_DC_ReceiptLines_Key')
CREATE INDEX IX_AP_DC_ReceiptLines_Key
    ON dbo.AP_DC_PurchaseReceiptLines (CLIENT_ID, COMPANY_ID, id);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_AP_DC_ReceiptLines_Doc')
CREATE INDEX IX_AP_DC_ReceiptLines_Doc
    ON dbo.AP_DC_PurchaseReceiptLines (CLIENT_ID, COMPANY_ID, documentId);
GO

-- the direct load keys COA rows by the BC account id ...
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_COA_Master_SAP_BcId')
CREATE INDEX IX_COA_Master_SAP_BcId
    ON dbo.COA_Master_SAP (CLIENT_ID, COMPANY_ID, ACCOUNT_NUMBER);
GO

-- ... and invoice posting looks accounts up by GL code
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_COA_Master_SAP_Key')
CREATE INDEX IX_COA_Master_SAP_Key
    ON dbo.COA_Master_SAP (CLIENT_ID, COMPANY_ID, ACCOUNT_CODE)
    INCLUDE (ACCOUNT_NUMBER);
GO


-- the invoice-posting poll, every 5 minutes

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_AP_DataConversions_Poll')
CREATE INDEX IX_AP_DataConversions_Poll
    ON dbo.AP_DataConversions (CLIENT_ID, COMPANY_ID, STATUS, ERP_POST)
    INCLUDE (RECORD_ID);
GO
