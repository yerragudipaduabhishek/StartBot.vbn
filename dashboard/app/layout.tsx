import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "BTC AI Trading",
  description: "Multi-agent BTC trading pipeline - live signals",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <nav className="topnav">
          <span className="topnav-brand">BTC AI TRADING</span>
          <Link href="/">Dashboard</Link>
          <Link href="/research">Research &amp; Governance</Link>
          <span className="topnav-note">PAPER ONLY · NO REAL ORDERS</span>
        </nav>
        {children}
      </body>
    </html>
  );
}
