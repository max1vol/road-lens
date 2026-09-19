CREATE TABLE `drafts` (
	`id` text PRIMARY KEY NOT NULL,
	`device_id` text NOT NULL,
	`session_id` text NOT NULL,
	`output` text NOT NULL,
	`turns` text NOT NULL,
	`run` text NOT NULL,
	`created_at` integer NOT NULL,
	`expires_at` integer NOT NULL,
	`status` text DEFAULT 'ready' NOT NULL,
	`consumed_report_id` text
);
--> statement-breakpoint
CREATE INDEX `draft_device_session` ON `drafts` (`device_id`,`session_id`);--> statement-breakpoint
CREATE TABLE `events` (
	`cursor` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`report_id` text NOT NULL,
	`type` text NOT NULL,
	`created_at` integer NOT NULL
);
--> statement-breakpoint
CREATE TABLE `jobs` (
	`id` text PRIMARY KEY NOT NULL,
	`kind` text NOT NULL,
	`report_id` text,
	`owner_id` text,
	`payload` text NOT NULL,
	`state` text DEFAULT 'queued' NOT NULL,
	`attempts` integer DEFAULT 0 NOT NULL,
	`lease_token` text,
	`lease_expires` integer,
	`result` text,
	`error` text,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `jobs_report_id_unique` ON `jobs` (`report_id`);--> statement-breakpoint
CREATE INDEX `jobs_claimable` ON `jobs` (`state`,`lease_expires`);--> statement-breakpoint
CREATE TABLE `rates` (
	`bucket` text PRIMARY KEY NOT NULL,
	`count` integer NOT NULL,
	`expires_at` integer NOT NULL
);
--> statement-breakpoint
CREATE TABLE `receipts` (
	`device_id` text NOT NULL,
	`key` text NOT NULL,
	`request_hash` text NOT NULL,
	`report_id` text NOT NULL,
	`received_at` integer NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `receipt_identity` ON `receipts` (`device_id`,`key`);--> statement-breakpoint
CREATE TABLE `reports` (
	`id` text PRIMARY KEY NOT NULL,
	`draft_id` text NOT NULL,
	`issue` text NOT NULL,
	`place_id` text NOT NULL,
	`received_at` integer NOT NULL,
	`analysis_status` text DEFAULT 'queued' NOT NULL,
	`review_status` text DEFAULT 'awaiting_officer_review' NOT NULL,
	`updated_at` integer NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `reports_draft_id_unique` ON `reports` (`draft_id`);