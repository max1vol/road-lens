import { sqliteTable, text, integer, uniqueIndex, index } from 'drizzle-orm/sqlite-core';
export const drafts = sqliteTable('drafts', {
 id:text('id').primaryKey(),deviceId:text('device_id').notNull(),sessionId:text('session_id').notNull(),output:text('output').notNull(),turns:text('turns').notNull(),run:text('run').notNull(),createdAt:integer('created_at').notNull(),expiresAt:integer('expires_at').notNull(),status:text('status').notNull().default('ready'),consumedReportId:text('consumed_report_id'),
},t=>[index('draft_device_session').on(t.deviceId,t.sessionId)]);
export const reports=sqliteTable('reports',{
 id:text('id').primaryKey(),draftId:text('draft_id').notNull().unique(),issue:text('issue').notNull(),placeId:text('place_id').notNull(),receivedAt:integer('received_at').notNull(),analysisStatus:text('analysis_status').notNull().default('queued'),reviewStatus:text('review_status').notNull().default('awaiting_officer_review'),updatedAt:integer('updated_at').notNull(),
});
export const receipts=sqliteTable('receipts',{
 deviceId:text('device_id').notNull(),key:text('key').notNull(),requestHash:text('request_hash').notNull(),reportId:text('report_id').notNull(),receivedAt:integer('received_at').notNull(),
},t=>[uniqueIndex('receipt_identity').on(t.deviceId,t.key)]);
export const jobs=sqliteTable('jobs',{
 id:text('id').primaryKey(),kind:text('kind').notNull(),reportId:text('report_id').unique(),ownerId:text('owner_id'),payload:text('payload').notNull(),state:text('state').notNull().default('queued'),attempts:integer('attempts').notNull().default(0),leaseToken:text('lease_token'),leaseExpires:integer('lease_expires'),result:text('result'),error:text('error'),createdAt:integer('created_at').notNull(),updatedAt:integer('updated_at').notNull(),
},t=>[index('jobs_claimable').on(t.state,t.leaseExpires)]);
export const events=sqliteTable('events',{
 cursor:integer('cursor').primaryKey({autoIncrement:true}),reportId:text('report_id').notNull(),type:text('type').notNull(),createdAt:integer('created_at').notNull(),
});
export const rates=sqliteTable('rates',{bucket:text('bucket').primaryKey(),count:integer('count').notNull(),expiresAt:integer('expires_at').notNull()});
