<script lang="ts">
	/**
	 * Fields of one sourced sustainability entry (a recycled/bio-based content object,
	 * a carbon footprint, a relative claim, or a size's container_details), rendered
	 * from its JSON schema so enums, descriptions and required markers have one source.
	 */
	import { UrlField, TagInput, Tooltip } from '$lib/components/form-fields';
	import { FixHint } from '$lib/components/ui';
	import { INPUT_COMPACT_CLASSES, LABEL_COMPACT_CLASSES } from '$lib/styles/formStyles';
	import { checkSourcedEntry } from '$lib/utils/dataQuality';
	import { enumLabel, schemaTypes, type Entry, type JsonSchema } from '$lib/utils/sustainability';

	interface Props {
		entry: Entry;
		/** JSON schema of the entry (an object schema with `properties`). */
		schema: JsonSchema | undefined;
		idPrefix: string;
		/** Numbers the source quote must contain, e.g. ['value_kg_co2e']. */
		valueFields: string[];
	}

	let { entry = $bindable(), schema, idPrefix, valueFields }: Props = $props();

	const WIDE = new Set(['source_quote', 'notes', 'baseline', 'source_url']);
	const TEXTAREA = new Set(['source_quote', 'notes']);

	let required = $derived(new Set(schema?.required ?? []));
	let fields = $derived(Object.entries(schema?.properties ?? {}));
	let problems = $derived(checkSourcedEntry(entry, valueFields));

	function label(key: string, prop: JsonSchema): string {
		return prop?.title ?? enumLabel(key);
	}

	// UrlField and TagInput have default values, and Svelte refuses to bind a field with
	// a default to `undefined`; a new entry has no URL or certifications yet, so those
	// bindings read through a default and write back to the entry.

	// Number inputs report '' while cleared; store null so pruning sees "no number".
	function setNumber(key: string, raw: string) {
		entry[key] = raw === '' ? null : Number(raw);
	}
</script>

<div class="grid grid-cols-1 gap-x-3 gap-y-2 sm:grid-cols-2">
	{#each fields as [key, prop] (key)}
		{@const id = `${idPrefix}-${key}`}
		{@const t = schemaTypes(prop)}
		<div class={WIDE.has(key) ? 'sm:col-span-2' : ''}>
			{#if key === 'source_url'}
				<UrlField
					bind:value={() => entry[key] ?? '', (v) => (entry[key] = v)}
					{id}
					label={label(key, prop)}
					required={required.has(key)}
					tooltip={prop.description ?? ''}
					compact
				/>
			{:else}
				<label for={id} class="{LABEL_COMPACT_CLASSES} flex items-center gap-1">
					{label(key, prop)}
					{#if required.has(key)}<span class="text-destructive">*</span>{/if}
					{#if prop.description}<Tooltip text={prop.description} />{/if}
				</label>
				{#if Array.isArray(prop.enum)}
					<select {id} bind:value={entry[key]} class={INPUT_COMPACT_CLASSES}>
						{#if !required.has(key)}<option value={undefined}>Not specified</option>{/if}
						{#each prop.enum as option (option)}
							<option value={option}>{enumLabel(String(option))}</option>
						{/each}
					</select>
				{:else if t.includes('boolean')}
					<input {id} type="checkbox" bind:checked={entry[key]} class="mt-1 h-4 w-4" />
				{:else if t.includes('number') || t.includes('integer')}
					<input
						{id}
						type="number"
						step={t.includes('integer') ? 1 : 'any'}
						min={prop.minimum}
						max={prop.maximum}
						value={entry[key] ?? ''}
						oninput={(e) => setNumber(key, e.currentTarget.value)}
						class={INPUT_COMPACT_CLASSES}
						placeholder={t.includes('null') ? 'Not stated' : ''}
					/>
				{:else if t.includes('array')}
					<TagInput
						bind:tags={() => entry[key] ?? [], (v) => (entry[key] = v)}
						placeholder="Add..."
					/>
				{:else if prop.format === 'date'}
					<input {id} type="date" bind:value={entry[key]} class={INPUT_COMPACT_CLASSES} />
				{:else if TEXTAREA.has(key)}
					<textarea {id} bind:value={entry[key]} rows="2" class={INPUT_COMPACT_CLASSES}></textarea>
				{:else}
					<input {id} type="text" bind:value={entry[key]} class={INPUT_COMPACT_CLASSES} />
				{/if}
			{/if}
		</div>
	{/each}
</div>

{#each problems as problem (problem)}
	<FixHint level="error" message={problem} compact class="mt-2" />
{/each}
