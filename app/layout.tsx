import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "RoadLens · Cambridge road safety",
  description: "Resident voice reports, dated road evidence, and officer review for Cambridge.",
  other: {
    "codex-preview": "development",
  },
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
