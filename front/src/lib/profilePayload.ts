import type { Sub2apiProfileInput } from "./api";

export function profileWritePayload(input: Partial<Sub2apiProfileInput>): Partial<Sub2apiProfileInput> {
  return {
    ...(input.name !== undefined && { name: input.name }),
    ...(input.site_key !== undefined && { site_key: input.site_key }),
    ...(input.promo_code !== undefined && { promo_code: input.promo_code }),
    ...(input.invitation_code !== undefined && { invitation_code: input.invitation_code }),
    ...(input.aff_code !== undefined && { aff_code: input.aff_code }),
    ...(input.enabled !== undefined && { enabled: input.enabled }),
  };
}
