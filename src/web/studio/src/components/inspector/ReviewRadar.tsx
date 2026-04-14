"use client";

import {
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  Radar,
  ResponsiveContainer,
} from "recharts";
import type { ReviewScore } from "@/types/schemas";

interface ReviewRadarProps {
  review: ReviewScore;
}

export function ReviewRadar({ review }: ReviewRadarProps) {
  const data = [
    { dimension: "Adherence", value: review.adherence },
    { dimension: "Pacing", value: review.pacing },
    { dimension: "Visual", value: review.visual_quality },
    { dimension: "Watch", value: review.watchability },
    { dimension: "Overall", value: review.overall },
  ];

  return (
    <ResponsiveContainer width="100%" height={180}>
      <RadarChart data={data} cx="50%" cy="50%" outerRadius="70%">
        <PolarGrid stroke="var(--border)" />
        <PolarAngleAxis
          dataKey="dimension"
          tick={{ fontSize: 9, fill: "var(--muted)" }}
        />
        <Radar
          dataKey="value"
          stroke="var(--accent)"
          fill="var(--accent)"
          fillOpacity={0.2}
        />
      </RadarChart>
    </ResponsiveContainer>
  );
}
