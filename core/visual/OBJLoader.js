
/**
 * @author mrdoob / http://mrdoob.com/
 * @author alteredq / http://alteredqualia.com/
 * @author Mugen87 / https://github.com/Mugen87
 */

THREE.OBJLoader = function ( manager ) {

	this.manager = ( manager !== undefined ) ? manager : THREE.DefaultLoadingManager;

};

THREE.OBJLoader.prototype = {

	constructor: THREE.OBJLoader,

	load: function ( url, onLoad, onProgress, onError ) {

		const scope = this;

		const loader = new THREE.FileLoader( scope.manager );
		loader.setPath( this.path );
		loader.load( url, function ( text ) {

			onLoad( scope.parse( text ) );

		}, onProgress, onError );

	},

	setPath: function ( value ) {

		this.path = value;
		return this;

	},

	parse: function ( text ) {
		const object = new THREE.Group();
		object.name = 'OBJGroup'; // for debugging

		let geometry, material, mesh;

		function parseVertexIndex( value ) {
			const index = parseInt( value );
			return ( index >= 0 ? index - 1 : index + vertices.length / 3 );
		}

		const vertices = [];
		const normals = [];
		const uvs = [];

		const lines = text.split( '\n' );

		for ( let i = 0; i < lines.length; i ++ ) {
			let line = lines[ i ].trim();
			if ( line.length === 0 || line.charAt( 0 ) === '#' ) continue;

			const parts = line.split( /\s+/ );
			const command = parts.shift();

			switch ( command ) {
				case 'v':
					vertices.push( parseFloat( parts[ 0 ] ), parseFloat( parts[ 1 ] ), parseFloat( parts[ 2 ] ) );
					break;
			}
		}

		const geometryOut = new THREE.BufferGeometry();
		geometryOut.setAttribute( 'position', new THREE.Float32BufferAttribute( vertices, 3 ) );

		const materialOut = new THREE.MeshBasicMaterial();
		const meshOut = new THREE.Mesh( geometryOut, materialOut );
		object.add( meshOut );

		return object;
	}
};
