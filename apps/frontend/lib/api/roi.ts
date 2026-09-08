import { apiClient } from './client';
import type { RoiEntry } from '../../types/types';

export interface RoiSummary {
  totalMonthlySavingsUsd: number;
  totalAnnualProjectedUsd: number;
  verifiedOptimizationsCount: number;
  averageLatencyReductionPct: number;
}

const EMPTY_SUMMARY: RoiSummary = {
  totalMonthlySavingsUsd: 0,
  totalAnnualProjectedUsd: 0,
  verifiedOptimizationsCount: 0,
  averageLatencyReductionPct: 0,
};

export const roiApi = {
  list: async (connectionId?: string | null): Promise<RoiEntry[]> => {
    // Hackathon patch: no mock-data fallback — an empty API result renders the
    // honest empty state instead of fabricated dollar figures.
    try {
      const endpoint = connectionId ? `/roi?connectionId=${connectionId}` : '/roi';
      const data = await apiClient.get<RoiEntry[]>(endpoint);
      return Array.isArray(data) ? data : [];
    } catch {
      return [];
    }
  },

  getSummary: async (): Promise<RoiSummary> => {
    try {
      const data = await apiClient.get<RoiSummary>('/roi/summary');
      return data ?? EMPTY_SUMMARY;
    } catch {
      return EMPTY_SUMMARY;
    }
  },
};