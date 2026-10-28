import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { entrySchema, newEntry, pruneEntry, pruneSustainability } from '../sustainability';

// The real schema, so these tests break if the schema and the editor drift apart.
const schema = JSON.parse(
	readFileSync(resolve(__dirname, '../../../../../schemas/sustainability_schema.json'), 'utf-8')
);

describe('newEntry', () => {
	it('starts unclear fields as unknown and stamps today as retrieved', () => {
		const entry = newEntry(entrySchema(schema, 'carbon_footprints'), '2026-10-26');
		expect(entry.declared_unit).toBe('unknown');
		expect(entry.boundary).toBe('unknown');
		expect(entry.packaging_included).toBe('unknown');
		expect(entry.source_type).toBe('primary');
		expect(entry.retrieved).toBe('2026-10-26');
		// No value is guessed.
		expect(entry.value_kg_co2e).toBeUndefined();
	});

	it('uses schema defaults', () => {
		expect(newEntry(entrySchema(schema, 'recycled_content')).pct_qualifier).toBe('exact');
	});
});

describe('pruneEntry', () => {
	it('keeps a required nullable pct as null and drops empty optional fields', () => {
		const out = pruneEntry(
			{ pct: '', type: 'post_industrial', notes: '  ', source_quote: ' made from scrap ' },
			entrySchema(schema, 'recycled_content')
		);
		expect(out).toEqual({ pct: null, type: 'post_industrial', source_quote: 'made from scrap' });
	});

	it('drops keys the schema does not know', () => {
		const out = pruneEntry(
			{ reduction_pct: 78, offset_trees: 5 },
			entrySchema(schema, 'relative_claims')
		);
		expect(out).toEqual({ reduction_pct: 78 });
	});
});

describe('pruneSustainability', () => {
	it('returns undefined for an empty block', () => {
		expect(pruneSustainability({}, schema)).toBeUndefined();
		expect(pruneSustainability({ carbon_footprints: [] }, schema)).toBeUndefined();
	});

	it('keeps sections that have entries', () => {
		const out = pruneSustainability(
			{ relative_claims: [{ reduction_pct: 78, metric: 'gwp' } as never] },
			schema
		);
		expect(out?.relative_claims?.[0]).toMatchObject({ reduction_pct: 78, metric: 'gwp' });
	});
});
