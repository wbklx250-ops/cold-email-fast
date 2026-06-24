"use client";

import React, { useState, useEffect } from "react";
import Step8SequencerUpload from "@/components/wizard/Step8SequencerUpload";
import CsvSequencerUpload from "@/components/wizard/CsvSequencerUpload";
import DomainMailboxExport from "@/components/mailboxes/DomainMailboxExport";

interface Batch {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
  tenant_count: number;
}

export default function SequencerUploaderPage() {
  const [mode, setMode] = useState<"batch" | "csv">("batch");
  const [batches, setBatches] = useState<Batch[]>([]);
  const [selectedBatchId, setSelectedBatchId] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

  useEffect(() => {
    fetchBatches();
  }, []);

  const fetchBatches = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/wizard/batches`);
      if (res.ok) {
        const data = await res.json();
        const batchList = Array.isArray(data) ? data : (data.batches || []);
        setBatches(batchList);
        if (batchList.length > 0 && !selectedBatchId) {
          setSelectedBatchId(batchList[0].id);
        }
      } else {
        setError("Failed to load batches");
      }
    } catch (err) {
      setError("Network error loading batches");
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Page Header */}
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-3">
            <span className="text-4xl">📤</span>
            Sequencer Upload
          </h1>
          <p className="mt-2 text-gray-600">
            Automatically upload mailboxes to your email sequencer (Instantly.ai or Smartlead.ai) using OAuth automation.
          </p>
        </div>

        {/* Mode Toggle Tabs */}
        <div className="bg-white rounded-lg border border-gray-200 p-1 mb-6 inline-flex">
          <button
            onClick={() => setMode("batch")}
            className={`px-5 py-2.5 rounded-md text-sm font-medium transition-colors ${
              mode === "batch"
                ? "bg-blue-600 text-white shadow-sm"
                : "text-gray-600 hover:text-gray-900 hover:bg-gray-100"
            }`}
          >
            📦 From Batch
          </button>
          <button
            onClick={() => setMode("csv")}
            className={`px-5 py-2.5 rounded-md text-sm font-medium transition-colors ${
              mode === "csv"
                ? "bg-blue-600 text-white shadow-sm"
                : "text-gray-600 hover:text-gray-900 hover:bg-gray-100"
            }`}
          >
            📄 From CSV
          </button>
        </div>

        {/* Batch Mode */}
        {mode === "batch" && (
          <>
            {/* Batch Selector */}
            <div className="bg-white rounded-lg border border-gray-200 p-6 mb-6">
              <label className="block text-sm font-medium text-gray-700 mb-3">
                Select Setup Batch
              </label>
              {loading ? (
                <div className="text-gray-500">Loading batches...</div>
              ) : error ? (
                <div className="text-red-600">{error}</div>
              ) : batches.length === 0 ? (
                <div className="text-gray-500">
                  No batches found. Create a batch in the{" "}
                  <a href="/setup" className="text-blue-600 hover:underline">
                    Setup Wizard
                  </a>{" "}
                  first.
                </div>
              ) : (
                <select
                  value={selectedBatchId}
                  onChange={(e) => setSelectedBatchId(e.target.value)}
                  className="w-full px-4 py-2.5 border border-gray-300 rounded-lg bg-white text-gray-900 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                >
                  {batches.map((batch) => (
                    <option key={batch.id} value={batch.id}>
                      {batch.name} ({batch.tenant_count} tenants) -{" "}
                      {new Date(batch.created_at).toLocaleDateString()}
                    </option>
                  ))}
                </select>
              )}
            </div>

            {/* Upload Component */}
            {selectedBatchId && (
              <div className="bg-white rounded-lg border border-gray-200 p-6">
                <Step8SequencerUpload
                  batchId={selectedBatchId}
                  suppressAutoComplete={true}
                />
              </div>
            )}

            {!selectedBatchId && !loading && batches.length > 0 && (
              <div className="bg-blue-50 border border-blue-200 rounded-lg p-6 text-center">
                <p className="text-blue-700">
                  Please select a batch above to begin uploading mailboxes to your sequencer
                </p>
              </div>
            )}
          </>
        )}

        {/* CSV Mode */}
        {mode === "csv" && (
          <div className="space-y-6">
            <DomainMailboxExport />
            <div className="bg-white rounded-lg border border-gray-200 p-6">
              <CsvSequencerUpload />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
