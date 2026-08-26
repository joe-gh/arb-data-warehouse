<?php
/**
 * Export a blog's curated product_cat tree + product memberships as two TSVs.
 *
 * Run on the WP box:
 *   sudo -u www-data wp eval-file export_curated_categories.php <blog_id> <out_dir>
 *
 * Writes <out_dir>/curated_categories.tsv and <out_dir>/curated_category_products.tsv
 * for the warehouse loader (infra/import-curated-categories.sh).
 *
 * Name cleaning: category names on the retail site carry double-encoded HTML
 * entity debris from an old bug ("Men's Pants &amp;amp" was once "Men's Pants &
 * Shorts" split on the ampersand). Decoding is repeated until stable, then a
 * dangling trailing "&" is dropped. Structure is exported as-is - merging the
 * split halves back together is a curation decision, not an import's.
 */

$args    = isset( $args ) && is_array( $args ) ? $args : array();
$blog_id = isset( $args[0] ) ? (int) $args[0] : 1;
$out_dir = isset( $args[1] ) ? rtrim( (string) $args[1], '/' ) : '/tmp';

switch_to_blog( $blog_id );

$clean = function ( $name ) {
    $name = (string) $name;
    for ( $i = 0; $i < 5; $i++ ) {
        $decoded = html_entity_decode( $name, ENT_QUOTES | ENT_HTML5, 'UTF-8' );
        if ( $decoded === $name ) {
            break;
        }
        $name = $decoded;
    }
    $name = trim( preg_replace( '/\s+/', ' ', $name ) );
    // Strip trailing entity debris left by the old split bug: "Batteries &amp",
    // "Men's Pants &", "... &amp;amp". Never touches an interior ampersand.
    $stripped = trim( preg_replace( '/(\s*&\s*(amp;?)*)+$/i', '', $name ) );
    return '' !== $stripped ? $stripped : $name;
};

$terms = get_terms( array( 'taxonomy' => 'product_cat', 'hide_empty' => false ) );
if ( is_wp_error( $terms ) ) {
    fwrite( STDERR, "get_terms failed: " . $terms->get_error_message() . "\n" );
    exit( 1 );
}
$by_id = array();
foreach ( $terms as $t ) {
    $by_id[ (int) $t->term_id ] = $t;
}
$depth_path = function ( $t ) use ( $by_id, $clean ) {
    $names = array( $clean( $t->name ) );
    $p     = (int) $t->parent;
    $d     = 0;
    while ( $p && isset( $by_id[ $p ] ) && $d < 10 ) {
        array_unshift( $names, $clean( $by_id[ $p ]->name ) );
        $p = (int) $by_id[ $p ]->parent;
        $d++;
    }
    return array( $d, implode( ' > ', $names ) );
};

$tsv = function ( $v ) {
    return str_replace( array( "\t", "\n", "\r" ), ' ', (string) $v );
};

global $wpdb;
// term_order is a plugin-added column (category-order plugins); core WP lacks it.
$orders = array();
if ( $wpdb->get_var( "SHOW COLUMNS FROM {$wpdb->terms} LIKE 'term_order'" ) ) {
    $orders = $wpdb->get_results(
        "SELECT term_id, term_order FROM {$wpdb->terms} WHERE term_order IS NOT NULL",
        OBJECT_K
    );
}

$fh = fopen( "{$out_dir}/curated_categories.tsv", 'w' );
foreach ( $terms as $t ) {
    list( $depth, $path ) = $depth_path( $t );
    $order = isset( $orders[ $t->term_id ] ) ? (int) $orders[ $t->term_id ]->term_order : 0;
    fwrite( $fh, implode( "\t", array(
        $blog_id,
        (int) $t->term_id,
        $tsv( $t->slug ),
        $tsv( $clean( $t->name ) ),
        (int) $t->parent,
        $depth,
        $tsv( $path ),
        $order,
        (int) $t->count,
    ) ) . "\n" );
}
fclose( $fh );

$rows = $wpdb->get_results( "
    SELECT tt.term_id, tr.object_id AS product_id,
           COALESCE(NULLIF(sku.meta_value, ''), NULLIF(style.meta_value, ''), '') AS sku
      FROM {$wpdb->term_relationships} tr
      JOIN {$wpdb->term_taxonomy} tt ON tt.term_taxonomy_id = tr.term_taxonomy_id
                                     AND tt.taxonomy = 'product_cat'
      JOIN {$wpdb->posts} p ON p.ID = tr.object_id
                            AND p.post_type = 'product' AND p.post_status = 'publish'
      LEFT JOIN {$wpdb->postmeta} sku   ON sku.post_id = p.ID   AND sku.meta_key = '_sku'
      LEFT JOIN {$wpdb->postmeta} style ON style.post_id = p.ID AND style.meta_key = 'product_style'
" );
$fh = fopen( "{$out_dir}/curated_category_products.tsv", 'w' );
$skipped = 0;
foreach ( $rows as $r ) {
    if ( '' === (string) $r->sku ) {
        $skipped++;
        continue;
    }
    fwrite( $fh, implode( "\t", array(
        $blog_id, (int) $r->term_id, $tsv( strtoupper( trim( $r->sku ) ) ), (int) $r->product_id,
    ) ) . "\n" );
}
fclose( $fh );

echo 'exported blog ' . $blog_id . ': ' . count( $terms ) . ' categories, '
    . ( count( $rows ) - $skipped ) . ' memberships'
    . ( $skipped ? " ({$skipped} skipped: product has no SKU)" : '' ) . "\n";
restore_current_blog();
