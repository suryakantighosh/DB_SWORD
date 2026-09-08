import { apiClient } from './client';
import type { Forecast, CalibrationBucket, MaePoint, BanditArm } from '../../types/types';

export interface ModelPerformanceResponse {
  calibration: CalibrationBucket[];
  mae: MaePoint[];
  bandit: BanditArm[];
}

const EMPTY_PERFORMANCE: ModelPerformanceResponse = {
  calibration: [],
  mae: [],
  bandit: [],
};

export const forecastsApi = {
  // Hackathon patch: no mock fallback — empty responses render the honest
  // empty state instead of fabricated forecast curves.
  list: async (): Promise<Forecast[]> => {
    try {
      const data = await apiClient.get<Forecast[]>('/forecasts');
      return Array.isArray(data) ? data : [];
    } catch {
      return [];
    }
  },

  getByConnectionId: async (connectionId: string): Promise<Forecast | null> => {
    try {
      return await apiClient.get<Forecast>(`/forecasts/${connectionId}`);
    } catch {
      return null;
    }
  },

  getModelPerformance: async (): Promise<ModelPerformanceResponse> => {
    try {
      const data = await apiClient.get<ModelPerformanceResponse>('/forecasts/models/performance');
      return data ?? EMPTY_PERFORMANCE;
    } catch {
      return EMPTY_PERFORMANCE;
    }
  },
};