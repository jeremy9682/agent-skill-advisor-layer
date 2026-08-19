import { fileURLToPath } from 'node:url'
import { FileSystemSkillProvider } from '@deepseek-ai/dsh-skill-filesystem'

export const name = 'dsh-dispatch-pack'
export const inject = ['skills']

export function apply(ctx) {
  const skillDir = fileURLToPath(new URL('./skills', import.meta.url))
  ctx.skills.registerProvider((control) =>
    new FileSystemSkillProvider(ctx, control, {
      providerName: 'dsh-dispatch-pack',
      customSkillDirs: [skillDir],
    }),
  )
}
