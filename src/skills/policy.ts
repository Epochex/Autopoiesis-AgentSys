import type { JsonObject } from "../core/types.js";
import type { Skill, SkillInvocation, SkillPermission } from "./types.js";

export interface SkillPolicyDecision {
  status: "allow" | "approval_required" | "deny";
  reason: string;
  permission?: SkillPermission;
}

export interface SkillPolicy {
  evaluate(skill: Skill, invocation: SkillInvocation): SkillPolicyDecision;
}

export class DefaultSkillPolicy implements SkillPolicy {
  evaluate(skill: Skill, invocation: SkillInvocation): SkillPolicyDecision {
    const approved = approvedPermissions(invocation.context.metadata);
    for (const permission of skill.permissions) {
      if (permission.risk === "privileged") {
        return {
          status: "deny",
          reason: `Privileged skill permission is denied by default: ${permission.permission}`,
          permission,
        };
      }
      if (permission.approval_required && !approved.has(permission.permission)) {
        return {
          status: "approval_required",
          reason: `Skill permission requires approval: ${permission.permission}`,
          permission,
        };
      }
    }
    return {
      status: "allow",
      reason: "All skill permissions allowed under current policy.",
    };
  }
}

function approvedPermissions(metadata: JsonObject): Set<string> {
  const value = metadata.approved_permissions;
  if (!Array.isArray(value)) return new Set();
  return new Set(value.map(String));
}
