"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

interface NavItem {
  name: string;
  href: string;
  icon: string;
}

const navigation: NavItem[] = [
  { name: "Batches", href: "/pipeline", icon: "📋" },
  { name: "New Batch", href: "/pipeline/new", icon: "🚀" },
  { name: "Dashboard", href: "/", icon: "📊" },
  { name: "Domains", href: "/domains", icon: "🌐" },
  { name: "Cloudflare Zones", href: "/cloudflare-zones", icon: "☁️" },
  { name: "Domain Lookup", href: "/domain-lookup", icon: "🔍" },
  { name: "Tenants", href: "/tenants", icon: "🏢" },
  { name: "Domain Removal", href: "/domain-removal", icon: "🗑️" },
  { name: "Sequencer Upload", href: "/instantly", icon: "📤" },
  { name: "Upload Manager", href: "/upload", icon: "📋" },
  { name: "Domain Checker", href: "/tools/domain-checker", icon: "🔎" },
];

interface SidebarProps {
  isOpen: boolean;
  onClose: () => void;
}

export function Sidebar({ isOpen, onClose }: SidebarProps) {
  const pathname = usePathname();

  return (
    <>
      {/* Mobile overlay */}
      {isOpen && (
        <div
          className="fixed inset-0 z-40 bg-gray-600 bg-opacity-75 lg:hidden"
          onClick={onClose}
        />
      )}

      {/* Sidebar */}
      <aside
        className={`fixed inset-y-0 left-0 z-50 w-64 bg-white border-r border-gray-200 transform transition-transform duration-300 ease-in-out lg:translate-x-0 lg:static lg:inset-auto ${
          isOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        {/* Logo/Brand */}
        <div className="flex h-16 items-center justify-between border-b border-gray-200 px-6">
          <Link href="/" className="flex items-center gap-2">
            <span className="text-2xl">📨</span>
            <span className="text-lg font-semibold text-gray-900">
              Cold Email
            </span>
          </Link>
          <button
            onClick={onClose}
            className="rounded-md p-2 text-gray-400 hover:bg-gray-100 hover:text-gray-500 lg:hidden"
          >
            <span className="sr-only">Close sidebar</span>
            <span className="text-xl">✕</span>
          </button>
        </div>

        {/* Navigation */}
        <nav className="flex flex-col gap-1 p-4">
          {navigation.map((item) => {
            const isActive =
              pathname === item.href ||
              (item.href !== "/" && pathname.startsWith(item.href));

            return (
              <Link
                key={item.name}
                href={item.href}
                onClick={onClose}
                className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-gray-100 text-gray-900"
                    : "text-gray-600 hover:bg-gray-50 hover:text-gray-900"
                }`}
              >
                <span className="text-xl">{item.icon}</span>
                {item.name}
              </Link>
            );
          })}
        </nav>

        {/* Footer */}
        <div className="absolute bottom-0 left-0 right-0 border-t border-gray-200 p-4">
          <div className="flex items-center gap-3 rounded-lg bg-gray-50 px-3 py-2.5">
            <span className="text-xl">⚙️</span>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-gray-900 truncate">
                Platform
              </p>
              <p className="text-xs text-gray-500">v1.0.0</p>
            </div>
          </div>
        </div>
      </aside>
    </>
  );
}

export function useSidebar() {
  const [isOpen, setIsOpen] = useState(false);
  return {
    isOpen,
    open: () => setIsOpen(true),
    close: () => setIsOpen(false),
    toggle: () => setIsOpen((prev) => !prev),
  };
}
